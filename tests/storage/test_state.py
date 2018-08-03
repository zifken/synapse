# -*- coding: utf-8 -*-
# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from twisted.internet import defer

from synapse.api.constants import EventTypes, Membership
from synapse.types import RoomID, UserID

import tests.unittest
import tests.utils

logger = logging.getLogger(__name__)


class StateStoreTestCase(tests.unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super(StateStoreTestCase, self).__init__(*args, **kwargs)
        self.store = None  # type: synapse.storage.DataStore

    @defer.inlineCallbacks
    def setUp(self):
        hs = yield tests.utils.setup_test_homeserver()

        self.store = hs.get_datastore()
        self.event_builder_factory = hs.get_event_builder_factory()
        self.event_creation_handler = hs.get_event_creation_handler()

        self.u_alice = UserID.from_string("@alice:test")
        self.u_bob = UserID.from_string("@bob:test")

        self.room = RoomID.from_string("!abc123:test")

        yield self.store.store_room(
            self.room.to_string(),
            room_creator_user_id="@creator:text",
            is_public=True
        )

    @defer.inlineCallbacks
    def inject_state_event(self, room, sender, typ, state_key, content):
        builder = self.event_builder_factory.new({
            "type": typ,
            "sender": sender.to_string(),
            "state_key": state_key,
            "room_id": room.to_string(),
            "content": content,
        })

        event, context = yield self.event_creation_handler.create_new_client_event(
            builder
        )

        yield self.store.persist_event(event, context)

        defer.returnValue(event)

    def assertStateMapEqual(self, s1, s2):
        for t in s1:
            # just compare event IDs for simplicity
            self.assertEqual(s1[t].event_id, s2[t].event_id)
        self.assertEqual(len(s1), len(s2))

    @defer.inlineCallbacks
    def test_get_state_for_event(self):

        # this defaults to a linear DAG as each new injection defaults to whatever
        # forward extremities are currently in the DB for this room.
        e1 = yield self.inject_state_event(
            self.room, self.u_alice, EventTypes.Create, '', {},
        )
        e2 = yield self.inject_state_event(
            self.room, self.u_alice, EventTypes.Name, '', {
                "name": "test room"
            },
        )
        e3 = yield self.inject_state_event(
            self.room, self.u_alice, EventTypes.Member, self.u_alice.to_string(), {
                "membership": Membership.JOIN
            },
        )
        e4 = yield self.inject_state_event(
            self.room, self.u_bob, EventTypes.Member, self.u_bob.to_string(), {
                "membership": Membership.JOIN
            },
        )
        e5 = yield self.inject_state_event(
            self.room, self.u_bob, EventTypes.Member, self.u_bob.to_string(), {
                "membership": Membership.LEAVE
            },
        )

        # check we get the full state as of the final event
        state = yield self.store.get_state_for_event(
            e5.event_id, None, filtered_types=None
        )

        self.assertIsNotNone(e4)

        self.assertStateMapEqual({
            (e1.type, e1.state_key): e1,
            (e2.type, e2.state_key): e2,
            (e3.type, e3.state_key): e3,
            # e4 is overwritten by e5
            (e5.type, e5.state_key): e5,
        }, state)

        # check we can filter to the m.room.name event (with a '' state key)
        state = yield self.store.get_state_for_event(
            e5.event_id, [(EventTypes.Name, '')], filtered_types=None
        )

        self.assertStateMapEqual({
            (e2.type, e2.state_key): e2,
        }, state)

        # check we can filter to the m.room.name event (with a wildcard None state key)
        state = yield self.store.get_state_for_event(
            e5.event_id, [(EventTypes.Name, None)], filtered_types=None
        )

        self.assertStateMapEqual({
            (e2.type, e2.state_key): e2,
        }, state)

        # check we can grab the m.room.member events (with a wildcard None state key)
        state = yield self.store.get_state_for_event(
            e5.event_id, [(EventTypes.Member, None)], filtered_types=None
        )

        self.assertStateMapEqual({
            (e3.type, e3.state_key): e3,
            (e5.type, e5.state_key): e5,
        }, state)

        # check we can use filter_types to grab a specific room member
        # without filtering out the other event types
        state = yield self.store.get_state_for_event(
            e5.event_id, [(EventTypes.Member, self.u_alice.to_string())],
            filtered_types=[EventTypes.Member],
        )

        self.assertStateMapEqual({
            (e1.type, e1.state_key): e1,
            (e2.type, e2.state_key): e2,
            (e3.type, e3.state_key): e3,
        }, state)

        # check that types=[], filtered_types=[EventTypes.Member]
        # doesn't return all members
        state = yield self.store.get_state_for_event(
            e5.event_id, [], filtered_types=[EventTypes.Member],
        )

        self.assertStateMapEqual({
            (e1.type, e1.state_key): e1,
            (e2.type, e2.state_key): e2,
        }, state)

        #######################################################
        # _get_some_state_from_cache tests against a full cache
        #######################################################

        room_id = self.room.to_string()
        group_ids = yield self.store.get_state_groups_ids(room_id, [e5.event_id])
        group = group_ids.keys()[0]

        # test _get_some_state_from_cache correctly filters out members with types=[]
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, True)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
            (e2.type, e2.state_key): e2.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members with wildcard types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, None)], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, True)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
            (e2.type, e2.state_key): e2.event_id,
            (e3.type, e3.state_key): e3.event_id,
            # e4 is overwritten by e5
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members with specific types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, e5.state_key)], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, True)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
            (e2.type, e2.state_key): e2.event_id,
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members with specific types
        # and no filtered_types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, e5.state_key)], filtered_types=None
        )

        self.assertEqual(is_all, True)
        self.assertDictEqual({
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)

        #######################################################
        # deliberately remove e2 (room name) from the _state_group_cache

        (is_all, known_absent, state_dict_ids) = self.store._state_group_cache.get(group)

        self.assertEqual(is_all, True)
        self.assertEqual(known_absent, set())
        self.assertDictEqual(state_dict_ids, {
            (e1.type, e1.state_key): e1.event_id,
            (e2.type, e2.state_key): e2.event_id,
            (e3.type, e3.state_key): e3.event_id,
            # e4 is overwritten by e5
            (e5.type, e5.state_key): e5.event_id,
        })

        state_dict_ids.pop((e2.type, e2.state_key))
        self.store._state_group_cache.invalidate(group)
        self.store._state_group_cache.update(
            sequence=self.store._state_group_cache.sequence,
            key=group,
            value=state_dict_ids,
            # list fetched keys so it knows it's partial
            fetched_keys=(
                (e1.type, e1.state_key),
                (e3.type, e3.state_key),
                (e5.type, e5.state_key),
            )
        )

        (is_all, known_absent, state_dict_ids) = self.store._state_group_cache.get(group)

        self.assertEqual(is_all, False)
        self.assertEqual(known_absent, set([
            (e1.type, e1.state_key),
            (e3.type, e3.state_key),
            (e5.type, e5.state_key),
        ]))
        self.assertDictEqual(state_dict_ids, {
            (e1.type, e1.state_key): e1.event_id,
            (e3.type, e3.state_key): e3.event_id,
            (e5.type, e5.state_key): e5.event_id,
        })

        ############################################
        # test that things work with a partial cache

        # test _get_some_state_from_cache correctly filters out members with types=[]
        room_id = self.room.to_string()
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, False)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members wildcard types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, None)], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, False)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
            (e3.type, e3.state_key): e3.event_id,
            # e4 is overwritten by e5
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members with specific types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, e5.state_key)], filtered_types=[EventTypes.Member]
        )

        self.assertEqual(is_all, False)
        self.assertDictEqual({
            (e1.type, e1.state_key): e1.event_id,
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)

        # test _get_some_state_from_cache correctly filters in members with specific types
        # and no filtered_types
        (state_dict, is_all) = yield self.store._get_some_state_from_cache(
            group, [(EventTypes.Member, e5.state_key)], filtered_types=None
        )

        self.assertEqual(is_all, True)
        self.assertDictEqual({
            (e5.type, e5.state_key): e5.event_id,
        }, state_dict)