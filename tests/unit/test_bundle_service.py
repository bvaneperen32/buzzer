"""
Unit tests for image references and game bundles (T8): image_refs and bundle_service.

Pure functions only — no database, Redis or running stack.

Run with:
    cd tests/unit && PYTHONPATH=../../backend pytest test_bundle_service.py -v
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from app.common.exceptions import ConflictError
from app.services import bundle_service as bs
from app.services.bundle_service import GameMeta, ImageRecord, QuestionFields
from app.services.image_refs import (
    CANVAS,
    OPTION,
    PROMPT,
    Ref,
    image_refs,
    map_image_refs,
)

# The version 1 samples, which must still export byte for byte as before T8. Version 2
# samples (T6 games with images) are round-tripped by the integration suite instead.
SAMPLE_GAMES = sorted(
    path
    for path in (Path(__file__).resolve().parents[2] / "sample_games").glob("*.json")
    if json.loads(path.read_text(encoding="utf-8"))["version"] == 1
)
META = GameMeta(title="Café quiz — ünïcode", description="A & B < C", max_players=150)


def question(config: dict, prompt: str = "Which one?") -> QuestionFields:
    return QuestionFields(
        type="multiple_choice",
        grading_type="ACCURACY",
        prompt=prompt,
        config=config,
        answer_data={"answer_points": [1000, 0]},
        time_limit_seconds=30,
        points_value=1000.0,
    )


def image(
    image_id: int,
    size: int = 5,
    *,
    version: int = 1,
    width: int = 1600,
    height: int = 900,
) -> ImageRecord:
    return ImageRecord(
        id=image_id,
        filename=f"pic{image_id}.png",
        description=f"Picture {image_id}",
        byte_size=size,
        version=version,
        width=width,
        height=height,
        data=bytes(n % 256 for n in range(size)),
    )


def export(meta, questions, images) -> bytes:
    return bs.serialize(bs.build_bundle(meta, questions, images, include_data=True))


def exported_config(questions, images, index: int = 0) -> dict:
    return json.loads(export(META, questions, images))["questions"][index]["config"]


def pre_t8_export(meta: GameMeta, questions: list[QuestionFields]) -> bytes:
    """game_management_service.export_game as it was before T8 (commit 686c312), copied."""
    bundle = {
        "format": "buzzer/game",
        "version": 1,
        "game": {
            "title": meta.title,
            "description": meta.description,
            "max_players": meta.max_players,
        },
        "questions": [
            {
                "type": q.type,
                "grading_type": q.grading_type,
                "prompt": q.prompt,
                "config": q.config,
                "answer_data": q.answer_data,
                "time_limit_seconds": q.time_limit_seconds,
                "points_value": q.points_value,
            }
            for q in questions
        ],
    }
    return json.dumps(bundle, indent=2, ensure_ascii=False).encode()


# ---------------------------------------------------------------------------
# image_refs / map_image_refs
# ---------------------------------------------------------------------------


class TestImageRefs:
    def test_every_role_in_order(self):
        config = {
            "image_id": 9,
            "option_images": [4, None, 5],
            "prompt_image_id": 3,
            "options": ["a", "b", "c"],
        }
        assert image_refs(config) == [
            Ref(3, PROMPT, None),
            Ref(4, OPTION, 0),
            Ref(5, OPTION, 2),
            Ref(9, CANVAS, None),
        ]

    @pytest.mark.parametrize(
        "value", [True, False, 0, -2, 2.0, "3", None, [3], {"id": 3}]
    )
    def test_only_positive_ints_count(self, value):
        config = {
            "prompt_image_id": value,
            "option_images": [value, 7],
            "image_id": value,
        }
        assert image_refs(config) == [Ref(7, OPTION, 1)]

    @pytest.mark.parametrize(
        "config", [None, [], "x", 5, {"option_images": "7"}, {"option_images": 7}]
    )
    def test_malformed_configs_have_no_refs(self, config):
        assert image_refs(config) == []


class TestMapImageRefs:
    def test_maps_every_slot_and_keeps_everything_else(self):
        config = {
            "options": ["a", "b"],
            "prompt_image_id": 1,
            "extra": {"nested": [1, 2]},
            "option_images": [2, None],
            "image_id": 3,
        }
        mapped = map_image_refs(config, lambda value: f"k{value}")
        assert mapped == {
            "options": ["a", "b"],
            "prompt_image_id": "k1",
            "extra": {"nested": [1, 2]},
            "option_images": ["k2", None],
            "image_id": "k3",
        }
        assert list(mapped) == list(
            config
        )  # key order kept, so serialization is stable
        assert config["option_images"] == [2, None]  # the input isn't mutated

    def test_fn_sees_every_non_null_value_whatever_it_is(self):
        seen = []
        config = {
            "prompt_image_id": "x",
            "option_images": [True, None, 2.5],
            "image_id": 0,
        }
        map_image_refs(config, lambda value: seen.append(value))
        assert seen == ["x", True, 2.5, 0]

    def test_nulls_stay_null_and_absent_keys_stay_absent(self):
        assert map_image_refs({"prompt_image_id": None}, lambda v: 1) == {
            "prompt_image_id": None
        }
        assert map_image_refs({"options": ["a"]}, lambda v: 1) == {"options": ["a"]}

    @pytest.mark.parametrize("value", ["abc", 5, {"a": 1}])
    def test_non_list_option_images_becomes_none(self, value):
        assert map_image_refs({"option_images": value}, lambda v: v) == {
            "option_images": None
        }

    def test_fn_errors_propagate(self):
        # Import relies on this to refuse unknown keys.
        def refuse(value):
            raise ValueError(value)

        with pytest.raises(ValueError, match="bad"):
            map_image_refs({"option_images": [None, "bad"]}, refuse)

    def test_non_dict_config_is_returned_unchanged(self):
        assert map_image_refs(["x"], lambda v: v) == ["x"]


# ---------------------------------------------------------------------------
# build_bundle / serialize
# ---------------------------------------------------------------------------


class TestBuildBundle:
    def test_keys_follow_id_order_not_input_order(self):
        images = [image(30), image(10), image(20)]
        bundle = bs.build_bundle(
            META,
            [question({"options": ["a", "b"], "prompt_image_id": 30})],
            images,
            include_data=True,
        )
        assert [(entry["key"], entry["filename"]) for entry in bundle["images"]] == [
            ("img-1", "pic10.png"),
            ("img-2", "pic20.png"),
            ("img-3", "pic30.png"),
        ]
        assert bundle["questions"][0]["config"]["prompt_image_id"] == "img-3"

    def test_version2_structure(self):
        images = [image(1, size=7)]
        questions = [
            question(
                {"prompt_image_id": 1, "options": ["", "b"], "option_images": [1, None]}
            )
        ]
        bundle = bs.build_bundle(META, questions, images, include_data=True)
        assert list(bundle) == ["format", "version", "game", "images", "questions"]
        assert bundle["version"] == 2
        [entry] = bundle["images"]
        assert list(entry) == ["key", "filename", "description", "data"]
        assert base64.b64decode(entry["data"], validate=True) == images[0].data
        assert bundle["questions"][0]["config"] == {
            "prompt_image_id": "img-1",
            "options": ["", "b"],
            "option_images": ["img-1", None],
        }
        # Exporting the same game twice gives identical bytes.
        assert export(META, questions, images) == export(META, questions, images)

    def test_measuring_leaves_data_empty(self):
        bundle = bs.build_bundle(META, [], [image(1)], include_data=False)
        assert bundle["images"][0]["data"] == ""

    def test_exporting_without_loaded_data_fails_loudly(self):
        with pytest.raises(ValueError):
            bs.build_bundle(META, [], [image(1)._replace(data=None)], include_data=True)

    def test_version1_matches_the_pre_t8_exporter(self):
        questions = [
            question(
                {"options": ["Paris", "Rome"], "extra": "kept"}, prompt="A &amp; B"
            ),
            QuestionFields(
                "fill_in_the_blank",
                "COMPLETENESS",
                "Name it",
                {},
                {"accepted_answers": ["x"]},
                20,
                500,
            ),
        ]
        assert bs.build_bundle(META, questions, [], include_data=True)["version"] == 1
        assert export(META, questions, []) == pre_t8_export(META, questions)

    @pytest.mark.parametrize("path", SAMPLE_GAMES, ids=lambda path: path.name)
    def test_sample_games_export_as_before(self, path):
        raw = json.loads(path.read_text(encoding="utf-8"))
        meta = GameMeta(
            raw["game"]["title"],
            raw["game"].get("description", ""),
            raw["game"].get("max_players", 150),
        )
        questions = [
            QuestionFields(
                q["type"],
                q["grading_type"],
                q["prompt"],
                q["config"],
                q["answer_data"],
                q.get("time_limit_seconds", 30),
                q.get("points_value", 1000),
            )
            for q in raw["questions"]
        ]
        assert export(meta, questions, []) == pre_t8_export(meta, questions)


# ---------------------------------------------------------------------------
# bundle_size / check_export
# ---------------------------------------------------------------------------


class TestBundleSize:
    @pytest.mark.parametrize("sizes", [[], [0], [1], [2], [3], [4], [5, 6, 7, 300]])
    def test_equals_the_export_length(self, sizes):
        images = [image(n + 1, size) for n, size in enumerate(sizes)]
        questions = [
            question({"options": ["a", ""], "option_images": [None, 1]})
            if images
            else question({"options": ["a", "b"]})
        ]
        assert bs.bundle_size(META, questions, images) == len(
            export(META, questions, images)
        )

    def test_measures_prompts_as_stored_after_sanitizing(self):
        from app.services.game_management_service import sanitize_prompt

        raw_prompt = "Is 1 < 2 & 3 > 2? <b>Yes</b> <script>no()</script>"
        stored = sanitize_prompt(raw_prompt)
        assert len(stored) != len(raw_prompt)  # sanitizing changes the length

        as_stored = [question({"options": ["a", "b"]}, prompt=stored)]
        measured = bs.bundle_size(META, as_stored, [])
        assert measured == len(export(META, as_stored, []))
        unsanitized = [question({"options": ["a", "b"]}, prompt=raw_prompt)]
        assert bs.bundle_size(META, unsanitized, []) != measured

    @pytest.mark.parametrize(
        "config",
        [
            {"prompt_image_id": "x", "option_images": "notalist", "image_id": [1]},
            {"options": "ab", "option_images": [True, {"a": 1}, -1, 2.0, 1]},
            {"options": [None, 5], "option_images": [99, 1, 1, 1]},
            {"prompt_image_id": {"deep": [1]}, "option_images": None},
            [],
            None,
        ],
    )
    def test_never_raises_on_malformed_references(self, config):
        questions = [question(config)]
        images = [image(1)]
        assert bs.bundle_size(META, questions, images) == len(
            export(META, questions, images)
        )


class TestCheckExport:
    LIMIT = bs.EXPORT_LIMIT_BYTES

    @pytest.mark.parametrize(
        ("before", "after"),
        [
            (100, 200),  # within the limit
            (LIMIT - 1, LIMIT),  # exactly at the limit
            (LIMIT + 50, LIMIT + 50),  # over, but not increased
            (LIMIT + 50, LIMIT + 10),  # over, and shrinking
        ],
    )
    def test_accepted(self, before, after):
        bs.check_export(before, after)

    @pytest.mark.parametrize(
        ("before", "after"), [(LIMIT, LIMIT + 1), (LIMIT + 5, LIMIT + 6)]
    )
    def test_refused_when_over_the_limit_and_increased(self, before, after):
        with pytest.raises(ConflictError) as caught:
            bs.check_export(before, after)
        assert f"{after:,}" in caught.value.message
        assert f"{self.LIMIT:,}" in caught.value.message
        assert caught.value.status_code == 409


# ---------------------------------------------------------------------------
# The export repairs for references that don't resolve (spec §2.7)
# ---------------------------------------------------------------------------


class TestExportRepairs:
    def test_unresolved_prompt_and_canvas_are_omitted(self):
        config = {"prompt_image_id": 999, "options": ["a", "b"], "image_id": 998}
        assert exported_config([question(config)], [image(1)]) == {
            "options": ["a", "b"]
        }

    def test_explicit_nulls_are_omitted(self):
        # Also why version 1 byte identity holds only for configs without image keys.
        config = {"prompt_image_id": None, "options": ["a"], "option_images": [None]}
        assert exported_config([question(config)], []) == {"options": ["a"]}

    def test_all_null_option_images_are_omitted(self):
        config = {"options": ["", "b"], "option_images": [999, None]}
        assert exported_config([question(config)], [image(1)]) == {"options": ["", "b"]}

    def test_non_list_option_images_are_omitted(self):
        config = {"options": ["a"], "option_images": "junk"}
        assert exported_config([question(config)], []) == {"options": ["a"]}

    def test_blank_caption_of_a_dropped_image_becomes_missing_image(self):
        config = {
            "options": ["", " ", "", "kept"],
            "option_images": [999, 1, None, "x"],
        }
        assert exported_config([question(config)], [image(1)]) == {
            "options": [bs.MISSING_IMAGE_CAPTION, " ", "", "kept"],
            "option_images": [None, "img-1", None, None],
        }
        assert config["options"] == ["", " ", "", "kept"]  # stored config untouched

    def test_another_games_image_is_unresolved(self):
        # Image 2 exists, but not in this game's library.
        config = {"prompt_image_id": 2, "options": ["a", ""], "option_images": [1, 2]}
        assert exported_config([question(config)], [image(1)]) == {
            "options": ["a", bs.MISSING_IMAGE_CAPTION],
            "option_images": ["img-1", None],
        }

    def test_repaired_questions_are_measured_exactly(self):
        questions = [
            question({"prompt_image_id": 999, "options": ["a", "b"]}),
            question({"options": ["", "b"], "option_images": [999, None]}),
            question({"options": ["", ""], "option_images": [999, 1]}),
        ]
        images = [image(1, 10)]
        assert bs.bundle_size(META, questions, images) == len(
            export(META, questions, images)
        )


# ---------------------------------------------------------------------------
# T7: Spot It in bundles — one portable representation (T7 spec §2.9)
# ---------------------------------------------------------------------------

SPOT_CELLS = [{"col": 4, "row": 1}, {"col": 1, "row": 0}]  # stored out of row-major order


def spot_question(
    answer_data: dict | None = None,
    *,
    cols: int = 6,
    rows: int = 6,
    image_id: int = 1,
    width: object = 1600,
    height: object = 900,
    grading: str = "ACCURACY",
) -> QuestionFields:
    return QuestionFields(
        type="find_in_image",
        grading_type=grading,
        prompt="Find the lighthouse",
        config={
            "image_id": image_id,
            "grid": {"cols": cols, "rows": rows},
            "width": width,
            "height": height,
        },
        answer_data={"accepted_cells": SPOT_CELLS} if answer_data is None else answer_data,
        time_limit_seconds=30,
        points_value=1000.0,
    )


def stamped(version: int = 1, cols: int = 6, rows: int = 6, image_id: int = 1) -> dict:
    return {
        "accepted_cells": SPOT_CELLS,
        "confirmed_for": {
            "image_id": image_id,
            "image_version": version,
            "cols": cols,
            "rows": rows,
        },
    }


def exported_question(questions, images, index: int = 0) -> dict:
    return json.loads(export(META, questions, images))["questions"][index]


class TestSpotItBundles:
    def test_confirmed_question_exports_the_flag_and_no_dimensions(self):
        exported = exported_question([spot_question(stamped())], [image(1)])
        assert exported["config"] == {"image_id": "img-1", "grid": {"cols": 6, "rows": 6}}
        assert exported["answer_data"] == {"accepted_cells": SPOT_CELLS, "confirmed": True}

    @pytest.mark.parametrize(
        ("question_", "images", "confirmed"),
        [
            (spot_question(stamped()), [image(1)], True),
            (spot_question(), [image(1)], False),  # not_confirmed
            (spot_question(stamped()), [image(1, version=2)], False),  # image_replaced
            (spot_question(stamped(image_id=2)), [image(1), image(2)], False),  # image_changed
            (spot_question(stamped(cols=5)), [image(1)], False),  # grid_changed
            (  # grid_too_dense: a 10×6 grid confirmed on landscape, image now portrait
                spot_question(stamped(version=2, cols=10, rows=6), cols=10, rows=6),
                [image(1, version=2, width=900, height=1600)],
                False,
            ),
            (spot_question(stamped(image_id=9), image_id=9), [image(1)], False),  # missing
            (  # replaced before reconfirmation: stored dimensions still the old image's
                spot_question(stamped(cols=10, rows=6), cols=10, rows=6),
                [image(1, version=2, width=900, height=1600)],
                False,
            ),
            (  # COMPLETENESS without cells, confirmed
                spot_question({"confirmed_for": stamped()["confirmed_for"]}, grading="COMPLETENESS"),
                [image(1)],
                True,
            ),
            (spot_question(stamped(), width="junk", height=None), [image(1)], True),
        ],
    )  # fmt: skip
    def test_every_review_status_is_measured_exactly(self, question_, images, confirmed):
        questions = [question(({"options": ["a", "b"]})), question_]
        assert bs.bundle_size(META, questions, images) == len(
            export(META, questions, images)
        )
        exported = exported_question(questions, images, index=1)
        assert ("confirmed" in exported["answer_data"]) is confirmed
        assert "width" not in exported["config"] and "height" not in exported["config"]
        assert "confirmed_for" not in exported["answer_data"]

    def test_pending_upload_record_is_measured_exactly(self):
        # add_image measures the library plus a pending record (version 1, processed size).
        questions = [spot_question(stamped())]
        pending = image(2, size=11, version=1, width=900, height=1600)
        assert bs.bundle_size(
            META, questions, [image(1), pending._replace(data=None)]
        ) == len(export(META, questions, [image(1), pending]))

    def test_substituted_replacement_record_drops_confirmed(self):
        # replace_image measures the library with the replaced record substituted: version
        # + 1 and the processed size. The stamp names version 1, so "confirmed" drops.
        questions = [spot_question(stamped())]
        before = [image(1, size=9)]
        after = [image(1, size=9, version=2, width=900, height=1600)]
        assert bs.bundle_size(META, questions, after) == len(export(META, questions, after))
        assert "confirmed" not in exported_question(questions, after)["answer_data"]
        c = bs.bundle_size(META, questions, before) - bs.bundle_size(META, questions, after)
        assert c == len(',\n        "confirmed": true')
        # Without the version increase the question would still measure as confirmed —
        # what T7's pinned replacement boundary test (§4.5, T2) detects.
        assert bs.bundle_size(META, questions, [before[0]._replace(width=900, height=1600)]) == (
            bs.bundle_size(META, questions, before)
        )

    def test_round_trip_representation_is_stable(self):
        # What an import rebuilds (dimensions from the image, a version-1 stamp when
        # confirmed) exports to the same bytes.
        questions = [spot_question(stamped(), width="x", height="y")]
        rebuilt = [spot_question(stamped())]
        assert export(META, questions, [image(1)]) == export(META, rebuilt, [image(1)])

    def test_sort_it_is_not_transformed(self):
        sort_q = QuestionFields(
            type="sort_into_boxes",
            grading_type="ACCURACY",
            prompt="Sort",
            config={"boxes": ["A", "B"], "cards": ["w", "x", "y", "z"]},
            answer_data={"correct_boxes": [0, 1, 0, 1]},
            time_limit_seconds=30,
            points_value=708.333,
        )
        exported = exported_question([sort_q], [])
        assert exported["config"] == sort_q.config
        assert exported["answer_data"] == sort_q.answer_data
        assert json.loads(export(META, [sort_q], []))["version"] == 1


# ---------------------------------------------------------------------------
# Reference shape rules in QuestionCreate.validate_structure (spec §2.7)
# ---------------------------------------------------------------------------


def question_body(type_: str = "multiple_choice", **config) -> dict:
    options = config.pop("options", ["Paris", "Rome"])
    body = {
        "type": type_,
        "grading_type": "ACCURACY",
        "prompt": "Which?",
        "config": {"options": options, **config},
        "answer_data": {"answer_points": [1] + [0] * (len(options) - 1)},
        "points_value": 1,
    }
    if type_ == "true_false":
        body["config"] = config
        body["answer_data"] = {"answer_points": {"true": 1, "false": 0}}
    elif type_ == "fill_in_the_blank":
        body["config"] = config
        body["answer_data"] = {"acceptedAnswers": ["x"], "answerPoints": [1]}
    return body


def refused(body: dict) -> str:
    from pydantic import ValidationError

    from app.schemas.admin import QuestionCreate

    with pytest.raises(ValidationError) as caught:
        QuestionCreate(**body)
    return str(caught.value)


def accepted(body: dict) -> None:
    from app.schemas.admin import QuestionCreate

    QuestionCreate(**body)


class TestQuestionImageRules:
    @pytest.mark.parametrize(
        "type_", ["multiple_choice", "true_false", "fill_in_the_blank", "multi_select"]
    )
    def test_prompt_image_on_every_type(self, type_):
        accepted(question_body(type_, prompt_image_id=5))
        accepted(question_body(type_, prompt_image_id=None))

    @pytest.mark.parametrize("value", [0, -1, "5", 2.0, True])
    def test_prompt_image_must_be_a_positive_int(self, value):
        assert "prompt_image_id" in refused(question_body(prompt_image_id=value))

    @pytest.mark.parametrize(
        "type_", ["multiple_choice", "true_false", "fill_in_the_blank", "multi_select"]
    )
    def test_image_id_is_refused_on_current_types_but_null_is_tolerated(self, type_):
        assert "only allowed on canvas question types" in refused(
            question_body(type_, image_id=5)
        )
        accepted(question_body(type_, image_id=None))

    @pytest.mark.parametrize("type_", ["true_false", "fill_in_the_blank"])
    def test_option_images_only_on_choice_types(self, type_):
        assert "option_images is only allowed" in refused(
            question_body(type_, option_images=[1])
        )

    def test_image_options_rules(self):
        accepted(question_body(options=["", "Rome"], option_images=[5, None]))
        accepted(question_body("multi_select", options=["", ""], option_images=[5, 6]))
        assert "one entry per option" in refused(question_body(option_images=[5]))
        assert "one entry per option" in refused(question_body(option_images="5"))
        assert "image ids" in refused(question_body(option_images=[5, True]))
        assert "Option B needs a caption or an image" in refused(
            question_body(options=["Paris", "  "], option_images=[5, None])
        )
        assert "must be text" in refused(
            question_body(options=["Paris", 7], option_images=[5, None])
        )

    def test_text_only_options_are_validated_exactly_as_before(self):
        # Blank and non-string options stay acceptable without option_images.
        accepted(question_body(options=["", 7]))
        accepted(question_body(options=["", 7], option_images=None))
