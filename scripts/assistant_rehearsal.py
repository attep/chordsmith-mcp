"""Rehearse the manager UI's Assistant flow against a live server and a local Ollama model.

Asks the model to build a small song through ChordSmith's tools and reports every call: what
the model sent, what the repair layer fixed (mirroring frontend/src/lib/arguments.ts), what the
server answered, and where a retry was needed. Run it after changes instead of clicking through
the Assistant call by call:

    python scripts/assistant_rehearsal.py --server https://your-host.example.com \
        --token-file monitor-token.json [--model qwen3.6:latest]

The schema simplifier and the repair rules here mirror the frontend (ollama.ts / arguments.ts);
keep them in sync when those change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
MAX_ROUNDS = 10
DEFAULT_PROMPT = (
    "Create a short instrumental song called {name}: a chord progression (Am, F, C, G), "
    "a backbeat drum track and a root_fifth bass line, then render it to MP3 in the background, "
    "poll until the render is done and tell me the file name."
)

SYSTEM_PROMPT = (
    "You are a music-production assistant connected to the ChordSmith MCP server. Use the tools to "
    "create and inspect MIDI and audio files; you choose the music, the tools write the files. Call "
    "tools with valid JSON arguments and summarise the results for the user. Follow the workflow in "
    "order: create the file first (create_chord_progression or create_progression_from_roman), then "
    "extend it (add_track, add_drums, add_bassline, set_track_instrument), then render it "
    "(render_audio or render_audio_job and poll get_render_job until done). Never call a tool on a "
    "file that does not exist yet. Complete the whole request in one go — do not stop to ask for "
    "confirmation; only reply when the render job is done."
)


# ------------------------------------------------------------------ schema helpers (mirror of schema.ts)


def resolve(schema: dict, root: dict, depth: int = 0) -> dict:
    current = schema or {}
    while depth < 16 and isinstance(current, dict) and current.get("$ref", "").startswith("#/"):
        ref = current["$ref"]
        node: object = root
        for part in ref[2:].split("/"):
            node = (node or {}).get(part.replace("~1", "/").replace("~0", "~")) if isinstance(node, dict) else None
        current = {**(node if isinstance(node, dict) else {}), **{k: v for k, v in current.items() if k != "$ref"}}
        depth += 1
    return current


def variants(schema: dict, root: dict) -> list[dict]:
    resolved = resolve(schema, root)
    listed = resolved.get("anyOf") or resolved.get("oneOf")
    if listed:
        return [
            option
            for option in (resolve(entry, root) for entry in listed)
            if not (option.get("type") == "null" or option.get("const") is None and option.get("type") is None)
        ]
    type_value = resolved.get("type")
    if isinstance(type_value, list):
        return [{**resolved, "type": entry} for entry in type_value if entry != "null"]
    return [resolved]


def primary_type(schema: dict) -> str | None:
    type_value = schema.get("type")
    if isinstance(type_value, list):
        return next((entry for entry in type_value if entry != "null"), None)
    if type_value:
        return type_value
    if schema.get("properties"):
        return "object"
    if schema.get("items"):
        return "array"
    if schema.get("enum"):
        return "number" if isinstance(schema["enum"][0], (int, float)) else "string"
    return None


KEEP = ("description", "enum", "minimum", "maximum", "format")


def simplify_schema(schema: dict, root: dict, depth: int = 0) -> dict:
    """Inline $refs, flatten unions to the richest alternative (mirror of ollama.ts)."""
    resolved = resolve(schema, root)
    out: dict = {key: resolved[key] for key in KEEP if resolved.get(key) is not None}
    default = resolved.get("default")
    if default is not None:
        hint = f"Default: {json.dumps(default)}."
        out["description"] = f"{out.get('description', '')} {hint}".strip()
    options = variants(resolved, root)
    if len(options) > 1:
        rich = next((option for option in options if option.get("properties") or option.get("items")), None)
        if rich:
            merged = simplify_schema(rich, root, depth + 1)
            out = {**merged, **{k: v for k, v in out.items() if k != "type"}}
            types = {primary_type(option) for option in options if primary_type(option)}
            out["type"] = next(iter(types)) if len(types) == 1 else sorted(types)
            return out
    single = options[0] if options else resolved
    if single.get("const") is not None:
        out["enum"] = [single["const"]]
    type_value = single.get("type")
    if isinstance(type_value, list):
        type_value = next((entry for entry in type_value if entry != "null"), None)
    if type_value:
        out["type"] = type_value
    if single.get("enum") and "enum" not in out:
        out["enum"] = single["enum"]
    if depth >= 6:
        return {"type": out.get("type", "object"), "description": out.get("description")}
    if single.get("properties"):
        out["type"] = "object"
        out["properties"] = {key: simplify_schema(child, root, depth + 1) for key, child in single["properties"].items()}
        if single.get("required"):
            out["required"] = single["required"]
    if single.get("items"):
        out["items"] = simplify_schema(single["items"], root, depth + 1)
    return out


def to_ollama_tool(tool: dict) -> dict:
    root = tool.get("inputSchema") or {}
    parameters = simplify_schema(root, root)
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": (tool.get("description") or "")[:1200],
            "parameters": {
                "type": "object",
                "properties": parameters.get("properties", {}),
                "required": parameters.get("required", []),
            },
        },
    }


# ------------------------------------------------------------------ repair (mirror of arguments.ts)


def strip_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    return match.group(1).strip() if match else text


def find_balanced(text: str, open_char: str, close_char: str) -> str | None:
    start = text.find(open_char)
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def parse_loose_object(text: str) -> dict | None:
    for candidate in (text, strip_fences(text), find_balanced(text, "{", "}")):
        if not candidate:
            continue
        for attempt in (candidate.strip(), re.sub(r",\s*([}\]])", r"\1", candidate.strip())):
            try:
                parsed = json.loads(attempt)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def parse_list_text(text: str) -> list | None:
    trimmed = text.strip()
    if not trimmed:
        return None
    bracket = trimmed if trimmed.startswith("[") else find_balanced(trimmed, "[", "]")
    if bracket:
        try:
            parsed = json.loads(bracket)
            if isinstance(parsed, list):
                return parsed
        except ValueError:
            parts = [
                part.strip().strip('"').strip("'") for part in bracket[1:-1].split(",") if part.strip()
            ]
            if parts:
                return parts
    if "," in trimmed:
        return [part.strip() for part in trimmed.split(",") if part.strip()]
    return [trimmed]


def coerce_value(value, schema: dict, root: dict, depth: int = 0):
    """Returns (value, changed)."""
    if depth > 6 or value is None:
        return value, False
    options = variants(schema, root)
    if isinstance(value, str):
        text = value.strip()
        for option in options:
            kind = primary_type(option)
            if kind == "array":
                parsed = parse_list_text(text)
                if parsed:
                    return [coerce_value(item, option.get("items") or {}, root, depth + 1)[0] for item in parsed], True
            if kind == "object":
                parsed = parse_loose_object(text)
                if parsed:
                    return coerce_value(parsed, option, root, depth + 1)[0], True
            if kind in ("number", "integer") and text and _is_number(text):
                return float(text) if kind == "number" else int(text), True
            if kind == "boolean" and text.lower() in ("true", "false"):
                return text.lower() == "true", True
        return value, False
    if isinstance(value, list):
        option = next((entry for entry in options if primary_type(entry) == "array"), None)
        if not option or not option.get("items"):
            return value, False
        items, changed = [], False
        for item in value:
            coerced, item_changed = coerce_value(item, option["items"], root, depth + 1)
            items.append(coerced)
            changed = changed or item_changed
        return items, changed
    if isinstance(value, dict):
        option = next((entry for entry in options if primary_type(entry) == "object"), None)
        if not option or not option.get("properties"):
            return value, False
        out, changed = {}, False
        for key, child in value.items():
            child_schema = option["properties"].get(key)
            if not child_schema:
                out[key] = child
                continue
            coerced, child_changed = coerce_value(child, child_schema, root, depth + 1)
            out[key] = coerced
            changed = changed or child_changed
        return out, changed
    return value, False


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for row in range(1, len(a) + 1):
        current = [row]
        for col in range(1, len(b) + 1):
            cost = 0 if a[row - 1] == b[col - 1] else 1
            current.append(min(current[col - 1] + 1, previous[col] + 1, previous[col - 1] + cost))
        previous = current
    return previous[-1]


def name_score(name: str, candidate: str) -> int:
    needle, hay = name.lower(), candidate.lower()
    if hay == needle:
        return 0
    if hay.startswith(needle) or needle.startswith(hay):
        return 1
    if needle in hay:
        return 2
    return 3 + levenshtein(needle, hay)


def closest_name(name: str, candidates: list[str]) -> str | None:
    """The accepted field an invented name probably meant ('file' -> 'filename')."""
    best, best_score = None, 10**9
    for candidate in candidates:
        score = name_score(name, candidate)
        if score < best_score:
            best, best_score = candidate, score
    return best if best_score <= 5 else None


def describe_field(name: str, schema: dict | None, root: dict) -> str:
    options = variants(schema or {}, root)
    described = []
    for option in options:
        if option.get("enum"):
            described.append("one of: " + ", ".join(str(entry) for entry in option["enum"]))
            continue
        kind = primary_type(option)
        if kind == "array":
            item_kind = primary_type(resolve(option.get("items") or {}, root))
            suffix = " of strings" if item_kind == "string" else " of objects" if item_kind == "object" else ""
            described.append(f"array{suffix}")
        elif kind == "integer":
            described.append("number")
        else:
            described.append(kind or "value")
    unique = list(dict.fromkeys(described))
    example = ', e.g. ["Am", "F", "C", "G"]' if any(entry.startswith("array") for entry in unique) else ""
    return f"{name} ({' or '.join(unique)}{example})"


FILENAME_ALIASES = {"file", "source", "path", "file_name", "filename", "midi_file"}


def prepare_arguments(tool: dict, raw) -> tuple[dict, list[str], str | None]:
    """(args, repaired, error) — mirrors frontend/src/lib/arguments.ts prepareToolArguments."""
    if raw is None:
        parsed: dict = {}
    elif isinstance(raw, str):
        parsed = parse_loose_object(raw) or {}
        if not parsed:
            return {}, [], f"{tool['name']}: the tool arguments were not valid JSON ({raw[:120]})."
    else:
        parsed = raw
    root = tool.get("inputSchema") or {}
    resolved = resolve(root, root)
    properties = resolved.get("properties") or {}
    required = resolved.get("required") or []
    args: dict = {}
    repaired: list[str] = []
    for key, value in parsed.items():
        if key in properties:
            args[key] = value
            continue
        lower = key.lower()
        alias = "filename" if "filename" in properties and lower != "filename" and lower in FILENAME_ALIASES else None
        guess = alias or closest_name(key, list(properties))
        strong = bool(alias) or (guess is not None and name_score(key, guess) <= 4)
        if guess and guess != key and strong and guess not in parsed:
            args[guess] = value
            repaired.append(f"{key}→{guess}")
        else:
            args[key] = value
    for key, value in list(args.items()):
        if key not in properties:
            continue
        coerced, changed = coerce_value(value, properties[key], root)
        if changed:
            args[key] = coerced
            repaired.append(key)
    for name in required:
        if args.get(name) is not None:
            continue
        schema = properties.get(name)
        if not schema:
            continue
        options = variants(resolve(schema, root), root)
        kind = next(
            (candidate for candidate in ("array", "object") if any(primary_type(o) == candidate for o in options)),
            None,
        )
        if not kind:
            continue
        open_char, close_char = ("[", "]") if kind == "array" else ("{", "}")
        for source, value in args.items():
            if not isinstance(value, str):
                continue
            block = find_balanced(value, open_char, close_char)
            if not block:
                continue
            found = parse_list_text(block) if kind == "array" else parse_loose_object(block)
            if found is None:
                continue
            args[name] = coerce_value(found, schema, root)[0]
            repaired.append(name)
            cleaned = re.sub(r"\s{2,}", " ", value.replace(block, "")).strip()
            if cleaned:
                args[source] = cleaned
                repaired.append(source)
            break
    missing = [name for name in required if args.get(name) is None]
    if missing:
        sent = ", ".join(parsed.keys()) if parsed else "(no fields)"
        unknown = [key for key in args if key not in properties]
        notes = []
        for key in unknown:
            guess = closest_name(key, list(properties))
            notes.append(f"{key} (did you mean {guess}?)" if guess and guess != key else key)
        unknown_note = f" (unknown here: {', '.join(notes)})" if notes else ""
        fields = "; ".join(describe_field(name, properties.get(name), root) for name in missing)
        accepted = ", ".join(list(properties)[:12]) + (", …" if len(properties) > 12 else "")
        error = (
            f"{tool['name']} was NOT executed and is still available — it needs {', '.join(missing)}. "
            f"This call sent: {sent}{unknown_note}. "
            f"Required: {fields}. Accepted fields: {accepted}. "
            "Call it again with each field as its own separate property."
        )
        return args, repaired, error
    return args, repaired, None


# ------------------------------------------------------------------ MCP plumbing


def compact_for_model(value, limit: int = 12000) -> str:
    """Elide base64 and long strings before feeding a tool result back (mirror of ollama.ts)."""

    def elide(node):
        if isinstance(node, dict):
            return {
                key: f"<{len(child)} characters omitted>"
                if key == "data_base64" and isinstance(child, str)
                else elide(child)
                for key, child in node.items()
            }
        if isinstance(node, list):
            return [elide(child) for child in node]
        if isinstance(node, str) and len(node) > 2000:
            return f"<{len(node)} characters omitted>"
        return node

    text = json.dumps(elide(value), ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "... (truncated)"


def parse_response(response: httpx.Response) -> dict:
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise RuntimeError(f"no data line in SSE response: {response.text[:200]}")
    return response.json()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # model text often has →, ≠, …
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--server", default="http://localhost:8100", help="ChordSmith HTTP server.")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434", help="Ollama base URL.")
    parser.add_argument("--model", default="qwen3.6:latest", help="Ollama model id.")
    parser.add_argument("--token", default="", help="Bearer token (OAuth servers).")
    parser.add_argument("--token-file", default="", help="monitor-token.json to refresh a token from.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="What to ask the model.")
    args = parser.parse_args()

    headers = {**MCP_HEADERS}
    if args.token:
        headers["Authorization"] = f"Bearer {args.token}"
    elif args.token_file:
        data = json.loads(Path(args.token_file).read_text(encoding="utf-8"))
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{data['server_url']}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": data["refresh_token"],
                    "client_id": data["client_id"],
                },
            )
            response.raise_for_status()
            fresh = response.json()
        data["refresh_token"] = fresh.get("refresh_token", data["refresh_token"])
        data["access_token"] = fresh["access_token"]
        Path(args.token_file).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        headers["Authorization"] = f"Bearer {data['access_token']}"

    with httpx.Client(timeout=600) as client:
        init = client.post(
            f"{args.server}/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "assistant-rehearsal", "version": "0.1"},
                },
            },
        )
        if init.status_code != 200:
            print("could not open an MCP session:", init.status_code, init.text[:200])
            return 1
        session = init.headers["mcp-session-id"]
        scoped = {**headers, "mcp-session-id": session}
        client.post(f"{args.server}/mcp", headers=scoped, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        instructions = (parse_response(init).get("result", {}).get("instructions") or "").strip()

        rpc_id = [1]

        def rpc(method: str, params: dict) -> dict:
            rpc_id[0] += 1
            return parse_response(
                client.post(
                    f"{args.server}/mcp",
                    headers=scoped,
                    json={"jsonrpc": "2.0", "id": rpc_id[0], "method": method, "params": params},
                )
            )

        tools = rpc("tools/list", {})["result"]["tools"]
        by_name = {tool["name"]: tool for tool in tools}
        ollama_tools = [to_ollama_tool(tool) for tool in tools]
        print(f"{len(tools)} tools loaded from {args.server}; model {args.model}")

        prompt = args.prompt.replace("{name}", f"rehearsal_{time.strftime('%Y%m%d_%H%M%S')}")
        messages: list[dict] = [
            # the UI sends its own prompt plus the server's workflow instructions (initialize)
            {"role": "system", "content": "\n\n".join(part for part in (SYSTEM_PROMPT, instructions) if part)},
            {"role": "user", "content": prompt},
        ]
        calls: list[dict] = []
        failures = 0
        for round_number in range(1, MAX_ROUNDS + 1):
            chat = client.post(
                f"{args.ollama}/api/chat",
                json={
                    "model": args.model,
                    "messages": messages,
                    "tools": ollama_tools,
                    "stream": False,
                    "options": {"temperature": 0, "seed": 7},
                },
            )
            chat.raise_for_status()
            reply = chat.json()["message"]
            messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": reply.get("tool_calls")})
            tool_calls = reply.get("tool_calls") or []
            if not tool_calls:
                print(f"\nfinal reply: {(reply.get('content') or '').strip()[:400]}")
                break
            for call in tool_calls:
                name = call["function"]["name"]
                tool = by_name.get(name)
                print(f"\nround {round_number}: {name}")
                if tool is None:
                    print("  UNKNOWN TOOL")
                    failures += 1
                    messages.append({"role": "tool", "content": f"No tool named '{name}'.", "tool_name": name})
                    continue
                prepared, repaired, error = prepare_arguments(tool, call["function"].get("arguments"))
                if error:
                    print(f"  rejected (sent back to the model): {error[:200]}")
                    calls.append({"tool": name, "status": "bad-arguments"})
                    failures += 1
                    messages.append({"role": "tool", "content": error, "tool_name": name})
                    continue
                if repaired:
                    print(f"  repaired: {', '.join(repaired)}")
                result = rpc("tools/call", {"name": name, "arguments": prepared})
                payload = result.get("result", {})
                is_error = bool(payload.get("isError"))
                text = " ".join(part.get("text", "") for part in payload.get("content", []) if part.get("type") == "text")
                status = "error" if is_error else "ok"
                print(f"  {status}: {text[:200]}")
                calls.append({"tool": name, "status": "ok" if not is_error else "tool-error"})
                if is_error:
                    failures += 1
                structured = payload.get("structuredContent")
                model_value = structured if structured is not None else text
                messages.append(
                    {
                        "role": "tool",
                        "content": compact_for_model({"isError": is_error, "result": model_value}),
                        "tool_name": name,
                    }
                )
        else:
            print(f"\nstopped after {MAX_ROUNDS} rounds")
            failures += 1

        client.delete(f"{args.server}/mcp", headers=scoped)

    print("\n--- rehearsal report ---")
    for entry in calls:
        print(f"  {entry['status']:<14} {entry['tool']}")
    normalized_failures = [entry for entry in calls if entry["status"] in ("bad-arguments", "tool-error")]
    print(f"{len(calls)} calls, {len(normalized_failures)} needing attention")
    return 1 if normalized_failures else 0


if __name__ == "__main__":
    sys.exit(main())
