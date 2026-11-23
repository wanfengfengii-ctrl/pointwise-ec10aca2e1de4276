from hashlib import sha256
from unittest import TestCase

from archive.hashstate import Sha256State


class Sha256StateTests(TestCase):
    def test_matches_hashlib_across_chunks(self):
        body = bytes(range(200)) * 3
        for chunk in (1, 5, 64, 65, 128):
            state = Sha256State()
            for index in range(0, len(body), chunk):
                state.update(body[index:index + chunk])
            self.assertEqual(state.hexdigest(), sha256(body).hexdigest())

    def test_export_reload_round_trip(self):
        body = bytes(range(200))
        for split in (0, 1, 7, 63, 64, 65, 100, 129, 200):
            state = Sha256State()
            state.update(body[:split])
            resumed = Sha256State.load(*state.export())
            resumed.update(body[split:])
            self.assertEqual(resumed.hexdigest(), sha256(body).hexdigest(), split)

    def test_block_aligned_partial_blocks_export_no_tail(self):
        state = Sha256State()
        state.update(bytes(64) * 2)
        _, count, tail = state.export()
        self.assertEqual(count, 128)
        self.assertEqual(tail, '')
