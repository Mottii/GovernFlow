from gateway.adapters.openai import ChatStreamTracker


def test_stream_tracker_preserves_frames_across_chunk_boundaries() -> None:
    frame = b'data: {"choices":[{"delta":{"content":"x"}}],"usage":null}\n\n'
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n'
    done = b"data: [DONE]\n\n"
    tracker = ChatStreamTracker(suppress_injected_usage=False)
    output = tracker.feed(frame[:15])
    output += tracker.feed(frame[15:] + usage[:20])
    output += tracker.feed(usage[20:] + done)
    assert b"".join(output) == frame + usage + done
    assert tracker.usage is not None
    assert (tracker.usage.input, tracker.usage.output) == (4, 2)

    suppressing = ChatStreamTracker(suppress_injected_usage=True)
    assert b"".join(suppressing.feed(frame + usage + done)) == frame + done
