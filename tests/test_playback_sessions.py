import importlib.util
import unittest
import uuid
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "server" / "aria_song_server.py"
SPEC = importlib.util.spec_from_file_location("aria_song_server_playback", MODULE_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SERVER)


class Clock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


class PlaybackSessionManagerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.manager = SERVER.PlaybackSessionManager(clock=self.clock)

    def sync(self, device_id, *, session_id="shared", state=None, last_command_id=0):
        return self.manager.sync(
            {
                "deviceID": device_id,
                "deviceName": device_id.title(),
                "platform": "test",
                "sessionID": session_id,
                "lastCommandID": last_command_id,
                "state": state,
            }
        )

    def test_first_device_hosts_and_second_device_controls_same_state(self):
        mac_state = {
            "trackID": "track-one",
            "queueTrackIDs": ["track-one", "track-two"],
            "elapsed": 42,
            "isPlaying": True,
            "isShuffleEnabled": False,
            "repeatMode": "all",
            "volume": 0.7,
        }

        mac = self.sync("mac", state=mac_state)
        phone = self.sync("phone", state={"trackID": "wrong"})

        self.assertEqual(mac["role"], "host")
        self.assertEqual(phone["role"], "controller")
        self.assertEqual(phone["hostName"], "Mac")
        self.assertEqual(phone["state"]["trackID"], "track-one")
        self.assertEqual(len(phone["devices"]), 2)

    def test_controller_commands_are_delivered_once_to_host(self):
        self.sync("mac")
        self.sync("phone")
        accepted = self.manager.enqueue_command(
            "shared",
            {"deviceID": "phone", "action": "seek", "position": 95.5},
        )

        host_update = self.sync("mac")
        self.assertEqual(host_update["commands"][0]["action"], "seek")
        self.assertEqual(host_update["commands"][0]["position"], 95.5)

        acknowledged = self.sync("mac", last_command_id=accepted["commandID"])
        self.assertEqual(acknowledged["commands"], [])

    def test_host_can_update_progress_without_resending_the_queue(self):
        first = self.sync(
            "mac",
            state={
                "trackID": "track-one",
                "queueTrackIDs": ["track-one", "track-two"],
                "elapsed": 10,
                "isPlaying": True,
            },
        )
        self.assertEqual(first["state"]["queueTrackIDs"], ["track-one", "track-two"])

        update = self.sync(
            "mac",
            state={
                "trackID": "track-one",
                "elapsed": 11,
                "isPlaying": True,
            },
        )

        self.assertEqual(update["state"]["queueTrackIDs"], ["track-one", "track-two"])
        self.assertEqual(update["state"]["elapsed"], 11)

    def test_controller_becomes_host_after_old_host_disappears(self):
        self.sync("mac")
        self.sync("phone")
        self.clock.now += SERVER.PLAYBACK_DEVICE_TTL_SECONDS + 0.1

        response = self.sync("phone")

        self.assertEqual(response["role"], "host")
        self.assertEqual(response["hostDeviceID"], "phone")

    def test_separate_session_can_play_independently(self):
        personal_session = str(uuid.uuid4())
        shared = self.sync("mac", state={"trackID": "shared-track", "isPlaying": True})
        personal = self.sync(
            "phone",
            session_id=personal_session,
            state={"trackID": "personal-track", "isPlaying": True},
        )

        self.assertEqual(shared["role"], "host")
        self.assertEqual(personal["role"], "host")
        self.assertFalse(personal["isShared"])
        self.assertEqual(personal["state"]["trackID"], "personal-track")

    def test_rejects_invalid_session_and_command(self):
        with self.assertRaisesRegex(ValueError, "session"):
            self.sync("phone", session_id="not-a-uuid")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.manager.enqueue_command(
                "shared",
                {"deviceID": "phone", "action": "eraseEverything"},
            )


if __name__ == "__main__":
    unittest.main()
