import pytest

from server_py.recording import NDJSONRecorder



@pytest.mark.asyncio

async def test_recorder_close_no_hang(tmp_path):

    """

    NDJSONRecorder.close() must not hang: sentinel None must be task_done()'d.

    """

    path = tmp_path / "tape.ndjson.gz"

    rec = NDJSONRecorder(str(path), meta={})

    # enqueue one real event

    rec._enqueue({"type": "quote", "bid": 1.0, "ask": 2.0})

    # close should complete promptly

    await rec.close()

    assert path.exists(), "Recording file was not created"

