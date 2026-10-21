"""Content limits per PLAN §6 (verified against Bot API docs):
text 1..4096, caption <=1024, album 2..10 (mixed ok), entities keep offsets.
Nothing truncated or split — oversize is rejected with an explanation.
"""
from __future__ import annotations

import pytest
from hypothesis import given, settings as hyp_settings, strategies as st

from poster_bot.domain.content import (
    ALBUM_MAX,
    ALBUM_MIN,
    CAPTION_MAX,
    TEXT_MAX,
    Content,
    Entity,
    ForwardSource,
    MediaRef,
)
from poster_bot.domain.errors import ContentValidationError


def _media(kind: str = "photo", caption: str | None = None) -> MediaRef:
    return MediaRef(kind=kind, file_id="f" + kind, caption=caption)


class TestText:
    def test_boundaries(self):
        Content(kind="text", text="x")
        Content(kind="text", text="x" * TEXT_MAX)
        with pytest.raises(ContentValidationError):
            Content(kind="text", text="")
        with pytest.raises(ContentValidationError) as exc_info:
            Content(kind="text", text="x" * (TEXT_MAX + 1))
        message = str(exc_info.value)
        assert str(TEXT_MAX + 1) in message and str(TEXT_MAX) in message
        assert "rejected" in message  # explanation, not truncation

    def test_entities_offsets_preserved_and_bounded(self):
        text = "hello owner"
        entity = Entity(type="bold", offset=6, length=5)
        content = Content(kind="text", text=text, entities=(entity,))
        restored = Content.from_json(content.to_json())
        assert restored.entities == (entity,)  # exact offsets survive storage
        with pytest.raises(ContentValidationError):
            Content(kind="text", text=text, entities=(Entity("bold", 8, 5),))
        with pytest.raises(ContentValidationError):
            Content(kind="text", text=text, entities=(Entity("bold", -1, 2),))


class TestMedia:
    def test_single_media_kinds(self):
        for kind in ("photo", "video", "audio"):
            Content(kind=kind, media=_media(kind))

    def test_caption_limit(self):
        Content(kind="photo", media=_media("photo", "c" * CAPTION_MAX))
        with pytest.raises(ContentValidationError):
            Content(kind="photo", media=_media("photo", "c" * (CAPTION_MAX + 1)))

    def test_kind_mismatch_rejected(self):
        with pytest.raises(ContentValidationError):
            Content(kind="photo", media=_media("video"))


class TestAlbum:
    def _items(self, count: int):
        return tuple(
            _media("photo" if i % 2 == 0 else "video") for i in range(count)
        )

    def test_min_max_and_mixed(self):
        Content(kind="album", items=self._items(ALBUM_MIN))
        Content(kind="album", items=self._items(ALBUM_MAX))  # mixed ok
        with pytest.raises(ContentValidationError):
            Content(kind="album", items=self._items(ALBUM_MIN - 1))
        with pytest.raises(ContentValidationError) as exc_info:
            Content(kind="album", items=self._items(ALBUM_MAX + 1))
        assert str(ALBUM_MAX + 1) in str(exc_info.value)  # not split

    def test_audio_not_an_album_item(self):
        with pytest.raises(ContentValidationError):
            Content(kind="album", items=self._items(1) + (_media("audio"),))

    def test_mixed_album_caption_and_item_captions_is_ambiguous(self):
        # an album-level caption alongside per-item captions is ambiguous:
        # rejected, never silently overwritten
        with pytest.raises(ContentValidationError):
            Content(
                kind="album",
                items=(_media("photo", "per-item"), _media("video")),
                caption="album caption",
            )

    def test_per_item_captions_are_valid(self):
        # distinct per-item captions are a legitimate album shape: each item
        # keeps its own caption+entities, cap 1024 each
        Content(
            kind="album",
            items=(
                _media("photo", "первая"),
                _media("video", "вторая"),
            ),
        )
        Content(kind="album", items=self._items(3), caption="album caption",
                caption_entities=(Entity("bold", 0, 5),))


class TestForward:
    def test_forward_carries_only_source(self):
        Content(
            kind="forward",
            mode="forward",
            forward=ForwardSource(chat_id=-100500, message_ids=(7, 8)),
        )
        with pytest.raises(ContentValidationError):
            Content(
                kind="forward",
                mode="forward",
                forward=ForwardSource(chat_id=-100500, message_ids=(7,)),
                text="extra",
            )

    def test_mode_source_coupling(self):
        with pytest.raises(ContentValidationError):
            Content(kind="text", text="x", mode="forward")
        with pytest.raises(ContentValidationError):
            Content(
                kind="forward",
                mode="copy",
                forward=ForwardSource(chat_id=-1, message_ids=(1,)),
            )

    def test_source_validation(self):
        with pytest.raises(ContentValidationError):
            ForwardSource(chat_id=0, message_ids=(1,))
        with pytest.raises(ContentValidationError):
            ForwardSource(chat_id=-1, message_ids=())


class TestStorageRoundTrip:
    def test_round_trip_all_kinds(self):
        samples = [
            Content(kind="text", text="t", entities=(Entity("italic", 0, 1),)),
            Content(kind="audio", media=_media("audio", "song")),
            Content(
                kind="album",
                items=(
                    MediaRef("photo", "a"),
                    MediaRef("video", "b"),
                ),
                caption="cap",
            ),
            Content(
                kind="album",
                items=(
                    MediaRef("photo", "a", caption="первая",
                             caption_entities=(Entity("bold", 0, 6),)),
                    MediaRef("video", "b", caption="вторая 🎬",
                             caption_entities=(
                                 Entity("custom_emoji", 7, 2,
                                        custom_emoji_id="123"),)),
                ),
            ),
            Content(
                kind="forward",
                mode="forward",
                forward=ForwardSource(-1001, (3, 4, 5)),
            ),
        ]
        for sample in samples:
            assert Content.from_json(sample.to_json()) == sample

    def test_garbage_rejected(self):
        with pytest.raises(ContentValidationError):
            Content.from_json("{not json")
        with pytest.raises(ContentValidationError):
            Content.from_json('{"kind": "mystery"}')
        with pytest.raises(ContentValidationError):
            Content.from_json('{"kind": "text", "entities": "nope"}')


@given(text=st.text(min_size=1, max_size=TEXT_MAX))
@hyp_settings(max_examples=60)
def test_any_valid_text_round_trips(text: str):
    content = Content(kind="text", text=text)
    assert Content.from_json(content.to_json()).text == text


@given(
    offset=st.integers(0, 50),
    length=st.integers(1, 20),
    text_len=st.integers(1, 90),
)
@hyp_settings(max_examples=120)
def test_entity_validation_matches_offset_arithmetic(offset, length, text_len):
    text = "x" * text_len
    fits = offset + length <= text_len
    if fits:
        Content(kind="text", text=text, entities=(Entity("bold", offset, length),))
    else:
        with pytest.raises(ContentValidationError):
            Content(kind="text", text=text, entities=(Entity("bold", offset, length),))


class TestUtf16EntityOffsets:
    """Entity offsets are UTF-16 code units (Bot API convention) and must land
    on character boundaries; text/caption limits stay character counts. The two
    measures are deliberately different and must never be confused."""

    ASTRAL = "a😀b"  # UTF-16 units: 'a'=0, '😀'=1..2, 'b'=3, total 4

    def test_utf16_length_counts_astral_as_two(self):
        from poster_bot.domain.content import utf16_length

        assert utf16_length(self.ASTRAL) == 4
        assert len(self.ASTRAL) == 3  # Python chars — the text-limit measure

    def test_entity_over_whole_astral_char_valid(self):
        Content(
            kind="text",
            text=self.ASTRAL,
            entities=(Entity("bold", 1, 2),),
        )

    def test_entity_splitting_surrogate_pair_rejected(self):
        for split in (Entity("bold", 1, 1), Entity("bold", 2, 1)):
            with pytest.raises(ContentValidationError) as exc_info:
                Content(kind="text", text=self.ASTRAL, entities=(split,))
            assert "UTF-16" in str(exc_info.value)

    def test_offsets_measured_in_utf16_not_python_chars(self):
        # '😀b' spans units 1..3 inclusive -> offset 1, length 3: valid under
        # UTF-16 arithmetic (1+3 <= 4) though it would overflow len() == 3.
        Content(kind="text", text=self.ASTRAL, entities=(Entity("italic", 1, 3),))
        # and past the UTF-16 end is rejected even though len() would "fit" it
        with pytest.raises(ContentValidationError):
            Content(kind="text", text=self.ASTRAL, entities=(Entity("bold", 3, 2),))

    def test_captions_validated_on_utf16_too(self):
        MediaRef(kind="photo", file_id="f", caption=self.ASTRAL,
                 caption_entities=(Entity("bold", 1, 2),))
        with pytest.raises(ContentValidationError):
            MediaRef(kind="photo", file_id="f", caption=self.ASTRAL,
                     caption_entities=(Entity("bold", 1, 1),))

    def test_text_char_limit_stays_in_characters(self):
        # documented decision: the 4096 limit is the PLAN §6 "characters"
        # measure (Python len), independent of UTF-16 entity offsets
        Content(kind="text", text="😀" * TEXT_MAX)
        with pytest.raises(ContentValidationError):
            Content(kind="text", text="😀" * (TEXT_MAX + 1))


class TestEntityPayloadPreservation:
    """Every optional MessageEntity field the Bot API defines survives the
    storage round trip so aiogram can rebuild an identical entity."""

    def test_user_payload(self):
        ent = Entity("user", 0, 5, user_id=42)
        restored = Content.from_json(
            Content(kind="text", text="hello", entities=(ent,)).to_json()
        )
        assert restored.entities == (ent,)

    def test_text_mention_payload(self):
        ent = Entity("text_mention", 0, 5, user_id=7, username="owner")
        restored = Content.from_json(
            Content(kind="text", text="hello", entities=(ent,)).to_json()
        )
        assert restored.entities[0].user_id == 7
        assert restored.entities[0].username == "owner"

    def test_custom_emoji_and_language_payloads(self):
        emoji = Entity("custom_emoji", 0, 2, custom_emoji_id="7234567890")
        pre = Entity("pre", 0, 10, language="python")
        restored = Content.from_json(
            Content(kind="text", text="abcdefghijk", entities=(emoji, pre)).to_json()
        )
        assert restored.entities[0].custom_emoji_id == "7234567890"
        assert restored.entities[1].language == "python"

    def test_malformed_user_id_rejected(self):
        with pytest.raises(ContentValidationError):
            Content.from_json(
                '{"kind": "text", "text": "hi", "entities":'
                ' [{"type": "user", "offset": 0, "length": 1,'
                ' "user_id": "42"}]}'
            )

    def test_non_string_optional_fields_dropped_not_crashed(self):
        restored = Content.from_json(
            '{"kind": "text", "text": "hi", "entities":'
            ' [{"type": "url", "offset": 0, "length": 1, "url": 5}]}'
        )
        assert restored.entities[0].url is None