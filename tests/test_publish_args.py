import pytest

from publish import parse_args, run


def test_defaults():
    a = parse_args(["--video", "v.mp4"])
    assert a.visibility == "private" and a.made_for_kids is False and a.allow_long is False


def test_channel_and_visibility():
    a = parse_args(["--video", "v.mp4", "--channel-handle", "@x", "--visibility", "public"])
    assert a.channel_handle == "@x" and a.visibility == "public"


@pytest.mark.asyncio
async def test_public_workflow_rejects_private_default_before_browser(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    args = parse_args(["--video", str(video), "--require-public"])
    assert await run(args) == 2
