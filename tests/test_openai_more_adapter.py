from gateway.adapters.openai_more import ResponsesStreamTracker, embeddings_usage, responses_usage


def test_responses_usage_excludes_cache_and_reasoning_from_base_counts() -> None:
    usage = responses_usage(
        {
            "usage": {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 10},
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 5},
            }
        }
    )
    assert usage is not None
    assert (usage.input, usage.output, usage.cache_read, usage.cache_write, usage.reasoning) == (
        70,
        25,
        20,
        10,
        5,
    )


def test_embeddings_usage_is_input_only() -> None:
    usage = embeddings_usage({"usage": {"prompt_tokens": 2, "total_tokens": 2}})
    assert usage is not None
    assert (usage.input, usage.output) == (2, 0)


def test_responses_stream_usage_requires_final_event() -> None:
    tracker = ResponsesStreamTracker()
    tracker.feed(b'event: response.created\ndata: {"type":"response.created"}\n\n')
    assert tracker.usage is None
    tracker.feed(
        b'event: response.completed\ndata: {"type":"response.completed","response":'
        b'{"usage":{"input_tokens":12,"output_tokens":3}}}\n\n'
    )
    assert tracker.usage is not None
    assert (tracker.usage.input, tracker.usage.output) == (12, 3)
