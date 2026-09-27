"""Validate the small, versioned student-question protocol."""

import re


_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class ProtocolError(ValueError):
    pass


def _object(value, keys, required, path):
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - keys:
        raise ProtocolError(f"{path} has missing or unsupported fields")


def _string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ProtocolError(f"{path} must be nonempty text")


def _id(value, path):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ProtocolError(f"{path} must be a stable identifier")


def validate_protocol(protocol):
    """Return a validated protocol; do not rewrite exact survey language."""
    _object(protocol, {"version", "title", "intro", "scales", "sections", "source_instrument"},
            {"version", "title", "intro", "scales", "sections"}, "protocol")
    if type(protocol["version"]) is not int or protocol["version"] != 1:
        raise ProtocolError("unsupported protocol version")
    _string(protocol["title"], "title")
    _string(protocol["intro"], "intro")
    if "source_instrument" in protocol:
        _string(protocol["source_instrument"], "source_instrument")
    scales = protocol["scales"]
    if not isinstance(scales, dict):
        raise ProtocolError("scales must be an object")
    for scale_id, choices in scales.items():
        _id(scale_id, "scale ID")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 7:
            raise ProtocolError(f"scale {scale_id} must have 2–7 choices")
        for index, choice in enumerate(choices, 1):
            _object(choice, {"value", "label"}, {"value", "label"}, "choice")
            if type(choice["value"]) is not int or choice["value"] != index:
                raise ProtocolError(f"scale {scale_id} values must be consecutive from 1")
            _string(choice["label"], "choice label")
    sections = protocol["sections"]
    if not isinstance(sections, list) or not sections:
        raise ProtocolError("sections must be a nonempty list")
    section_ids = set()
    item_ids = set()
    for section in sections:
        _object(section, {"id", "title", "items"}, {"id", "title", "items"}, "section")
        _id(section["id"], "section ID")
        _string(section["title"], "section title")
        if section["id"] in section_ids:
            raise ProtocolError("duplicate section ID")
        section_ids.add(section["id"])
        if not isinstance(section["items"], list) or not section["items"]:
            raise ProtocolError("section items must be nonempty")
        for item in section["items"]:
            _object(item, {
                "id", "prompt", "wording", "response", "reflection_goal",
                "coverage_targets", "example_probes", "max_additional_probes",
            }, {"id", "prompt", "wording", "response", "reflection_goal"}, "item")
            _id(item["id"], "item ID")
            if item["id"] in item_ids:
                raise ProtocolError("duplicate item ID")
            item_ids.add(item["id"])
            _string(item["prompt"], "item prompt")
            _string(item["reflection_goal"], "reflection goal")
            if item["wording"] not in ("exact", "adaptive"):
                raise ProtocolError("wording must be exact or adaptive")
            response = item["response"]
            _object(response, {"kind", "scale_id"}, {"kind"}, "response")
            if response["kind"] == "likert":
                if response.get("scale_id") not in scales:
                    raise ProtocolError("Likert item refers to an unknown scale")
            elif response["kind"] == "text":
                if "scale_id" in response:
                    raise ProtocolError("text response cannot specify a scale")
            else:
                raise ProtocolError("unsupported response kind")
            targets = item.get("coverage_targets", [])
            if not isinstance(targets, list):
                raise ProtocolError("coverage_targets must be a list")
            target_ids = set()
            for target in targets:
                _object(target, {"id", "description"}, {"id", "description"}, "coverage target")
                _id(target["id"], "coverage target ID")
                _string(target["description"], "coverage target description")
                if target["id"] in target_ids:
                    raise ProtocolError("duplicate coverage target ID")
                target_ids.add(target["id"])
            probes = item.get("example_probes", [])
            if not isinstance(probes, list):
                raise ProtocolError("example_probes must be a list")
            for probe in probes:
                _string(probe, "example probe")
            limit = item.get("max_additional_probes", 2)
            if type(limit) is not int or not 0 <= limit <= 5:
                raise ProtocolError("max_additional_probes must be 0–5")
    return protocol
