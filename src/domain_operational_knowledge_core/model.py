"""Bounded public input and error vocabulary; no implicit coercion."""
from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "operational-knowledge-v1"
MAX_REQUEST = 262144
MAX_RESPONSE = 1048576
MAX_TEXT = 65536
MAX_MATERIAL = 262144
LEGACY_OPERATIONS = {"register_material", "publish_material", "propose", "review"}
LIFECYCLE_OPERATIONS = {"start_use", "complete_use", "correct_feedback", "propose_revision", "retire"}
OPERATIONS = LEGACY_OPERATIONS | LIFECYCLE_OPERATIONS
PUBLICATIONS = {"publish_material", "complete_use", "correct_feedback"}


class KnowledgeError(Exception):
    def __init__(self, code, message, commit_state="not_committed", *, details=None):
        super().__init__(message)
        self.code, self.message, self.commit_state = code, message, commit_state
        self.details = details or {}

    def as_dict(self):
        return dict(status="ERROR", code=self.code, message=self.message,
                    commit_state=self.commit_state, details=self.details)


def require(condition, message, code="INVALID_INPUT"):
    if not condition:
        raise KnowledgeError(code, message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def text(value, *, maximum=512, empty=False):
    require(type(value) is str, "Expected a string")
    try:
        raw = value.encode("utf-8")
    except UnicodeError as exc:
        raise KnowledgeError("INVALID_INPUT", "Invalid Unicode string") from exc
    require((empty or len(raw) > 0) and len(raw) <= maximum and "\x00" not in value,
            "String length or content exceeds its contract")
    return value


def identity(value):
    text(value, maximum=128)
    require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,127}", value) is not None, "Invalid stable ID")
    return value


def request_scope(value):
    return text(value, maximum=256)


def fields(value, names):
    require(type(value) is dict and set(value) == set(names.split()), "Unexpected or missing fields")


def context(value):
    require(type(value) is dict and len(value) <= 16, "Context must have at most 16 fields")
    for key, item in value.items():
        text(key, maximum=32)
        require(re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key) is not None, "Invalid context key")
        text(item, maximum=256)


def refs(value):
    require(type(value) is list and 1 <= len(value) <= 8, "Expected one to eight unique material IDs")
    for item in value:
        identity(item)
    require(len(set(value)) == len(value), "Duplicate reference")


def sha(value):
    require(type(value) is str and re.fullmatch(r"[a-f0-9]{64}", value) is not None, "Invalid SHA-256")


def reference(value):
    fields(value, "artifact_id version_id entries")
    for key in ("artifact_id", "version_id"):
        text(value[key], maximum=256)
    entries = value["entries"]
    require(type(entries) is list and 1 <= len(entries) <= 8, "Material needs one to eight entries")
    keys, blobs = set(), {}
    for item in entries:
        fields(item, "entry_key blob_ref sha256 byte_length")
        text(item["entry_key"], maximum=512)
        text(item["blob_ref"], maximum=256)
        sha(item["sha256"])
        require(type(item["byte_length"]) is int and 0 <= item["byte_length"] <= MAX_TEXT,
                "Material blob exceeds 64 KiB", "LIMIT")
        require(item["entry_key"] not in keys, "Duplicate entry key")
        keys.add(item["entry_key"])
        descriptor = (item["sha256"], item["byte_length"])
        require(item["blob_ref"] not in blobs or blobs[item["blob_ref"]] == descriptor,
                "Conflicting blob descriptors")
        blobs[item["blob_ref"]] = descriptor
    require(sum(item[1] for item in blobs.values()) <= MAX_MATERIAL, "Material exceeds 256 KiB", "LIMIT")


def decode(raw, *, maximum=MAX_REQUEST):
    require(type(raw) is bytes and len(raw) <= maximum, "Input must be bounded UTF-8 bytes", "LIMIT")

    def pairs(items):
        out = {}
        for key, item in items:
            require(key not in out, "Duplicate JSON field")
            out[key] = item
        return out

    def invalid(_):
        raise KnowledgeError("INVALID_INPUT", "Non-integer JSON numbers are unsupported")

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_float=invalid, parse_constant=invalid)
        count = 0

        def visit(item, depth):
            nonlocal count
            count += 1
            require(depth <= 12 and count <= 10000, "JSON structure limit exceeded", "LIMIT")
            if type(item) is str:
                item.encode("utf-8")
            elif type(item) is dict:
                for key, child in item.items():
                    visit(key, depth + 1)
                    visit(child, depth + 1)
            elif type(item) is list:
                for child in item:
                    visit(child, depth + 1)
            elif type(item) is int:
                require(abs(item) <= 9007199254740991, "Integer out of range")
        visit(value, 0)
        return value
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise KnowledgeError("INVALID_INPUT", "Invalid UTF-8 JSON") from exc


def request(raw):
    value = decode(raw)
    fields(value, "schema scope operation request_id body")
    require(value["schema"] == SCHEMA, "Unsupported request schema", "UNSUPPORTED_VERSION")
    request_scope(value["scope"])
    identity(value["request_id"])
    operation, body = value["operation"], value["body"]
    validate_body(operation, body)
    return value


def validate_body(operation, body):
    """Validate domain input independently of a serialized request envelope."""
    require(type(operation) is str and operation in OPERATIONS, "Unsupported operation")
    if operation in ("register_material", "publish_material"):
        fields(body, "material_id source_ref task_ref context claimed_outcome gaps " +
               ("reference" if operation == "register_material" else "text"))
        identity(body["material_id"])
        text(body["source_ref"])
        text(body["task_ref"])
        require(not any(ord(char) < 32 or ord(char) == 127 for char in body["task_ref"]),
                "Task reference cannot contain control characters")
        context(body["context"])
        require(body["claimed_outcome"] in ("SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"), "Invalid outcome")
        require(type(body["gaps"]) is list and len(body["gaps"]) <= 16, "Invalid gaps")
        for gap in body["gaps"]:
            text(gap)
        if operation == "register_material":
            reference(body["reference"])
        else:
            text(body["text"], maximum=MAX_TEXT, empty=True)
    elif operation in ("propose", "propose_revision"):
        fields(body, "proposal_id kind content conditions material_ids" +
               (" expected_version_id" if operation == "propose_revision" else ""))
        if operation == "propose_revision":
            identity(body["expected_version_id"])
        identity(body["proposal_id"])
        text(body["kind"], maximum=64)
        text(body["content"], maximum=MAX_TEXT)
        context(body["conditions"])
        refs(body["material_ids"])
    elif operation == "review":
        fields(body, "candidate_id candidate_sha256 decision reviewer reason evidence_material_ids")
        identity(body["candidate_id"])
        sha(body["candidate_sha256"])
        require(body["decision"] in ("ACCEPT", "REJECT"), "Invalid review decision")
        text(body["reviewer"])
        text(body["reason"], maximum=4096)
        refs(body["evidence_material_ids"])

    elif operation == "start_use":
        fields(body, "usage_id consumer_ref task_ref version_id context")
        identity(body["usage_id"])
        identity(body["version_id"])
        text(body["consumer_ref"])
        text(body["task_ref"])
        require(not any(ord(c) < 32 or ord(c) == 127 for c in body["task_ref"]), "Invalid task reference")
        context(body["context"])
    elif operation in ("complete_use", "correct_feedback"):
        fields(body, "usage_id material" + (" expected_feedback_id" if operation == "correct_feedback" else ""))
        identity(body["usage_id"])
        if operation == "correct_feedback":
            identity(body["expected_feedback_id"])
        validate_body("publish_material", body["material"])
    elif operation == "retire":
        fields(body, "expected_version_id reviewer reason evidence_material_ids")
        identity(body["expected_version_id"])
        text(body["reviewer"])
        text(body["reason"], maximum=4096)
        refs(body["evidence_material_ids"])


def material_body(request):
    return request["body"]["material"] if request["operation"] in ("complete_use", "correct_feedback") else request["body"]


def operation_key(scope, operation, request_id):
    request_scope(scope)
    require(type(operation) is str and operation in OPERATIONS, "Unsupported operation")
    identity(request_id)
    return digest([scope, operation, request_id])


def output(value):
    require(len(canonical(value)) + 1 <= MAX_RESPONSE, "Response exceeds 1 MiB", "LIMIT")
    return value
