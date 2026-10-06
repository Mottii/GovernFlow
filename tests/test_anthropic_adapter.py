from gateway.adapters.anthropic import MessageStreamTracker, message_usage


def test_anthropic_usage_keeps_cache_tokens_separate() -> None:
    usage = message_usage(
        {
            "usage": {
                "input_tokens": 25,
                "output_tokens": 15,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 30,
            }
        }
    )
    assert usage is not None
    assert (usage.input, usage.output, usage.cache_read, usage.cache_write) == (25, 15, 100, 30)


def test_anthropic_stream_uses_final_cumulative_usage() -> None:
    tracker = MessageStreamTracker()
    tracker.feed(
        b'event: message_start\ndata: {"type":"message_start","message":{"usage":'
        b'{"input_tokens":25,"output_tokens":1}}}\n\n'
    )
    tracker.feed(
        b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":15}}\n\n'
    )
    assert tracker.usage is None
    tracker.feed(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    assert tracker.usage is not None
    assert (tracker.usage.input, tracker.usage.output) == (25, 15)


def test_interrupted_anthropic_stream_has_no_final_usage() -> None:
    tracker = MessageStreamTracker()
    tracker.feed(
        b'event: message_start\ndata: {"type":"message_start","message":'
        b'{"usage":{"input_tokens":25,"output_tokens":1}}}\n\n'
    )
    assert tracker.usage is None
