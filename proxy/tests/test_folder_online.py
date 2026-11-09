from proxy.app import build_online_track


def test_virtual_audio_path_is_filename_with_actual_format():
    item = {"source": "lx", "id": "lx:kg:song/escaped", "title": "Song", "artist": "Artist", "ext": "flac", "audio_spec": {"path": "/actual/unsafe.mp3"}}
    track = build_online_track(item)
    path = track["audioSpec"]["path"]
    assert "/" not in path and "\\" not in path
    assert path.endswith(".flac")
    assert track["audioSpec"]["format"] == "flac"
    assert track["guid"].startswith("online:")
