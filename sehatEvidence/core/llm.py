from __future__ import annotations
import json
import re
import os
import requests
from typing import Any, Optional


class LLMerror(Exception):
    """used when there is failure in responding to the request"""
    pass


class LLMclient():
    def __init__(self,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 model: Optional[str] = None,
                 time_out: Optional[int] = 60,
                 max_tokens: Optional[int] = None,
                 extra_body: Optional[dict] = None,
                 ):
        # 1. Set the URL. .rstrip("/") just removes any accidental trailing slashes so our URLs don't break later.
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1")).rstrip("/")

        # 2. Get the API Key (password) from the variables or the operating system
        self.api_key = api_key or os.environ.get("LLM_API_KEY")

        # 3. Choose the model, defaulting to 'qwen-plus' if nothing else is provided
        self.model = model or os.environ.get("LLM_MODEL", "qwen-plus")

        # 4. Save the timeout limit (how long we wait for the AI to respond before giving up)
        # FIX: was self.time_out here but self.timeout in _chat() -- picked ONE name
        # (timeout, no underscore) and used it consistently everywhere below.
        self.timeout = time_out

        # 5. Open a reusable web session. This makes multiple requests faster.
        self.session = requests.Session()

        # 6. Cap on the model's own reply length. Previously never sent at
        # all, so the endpoint's own (unadvertised, possibly small) default
        # applied -- which can silently truncate a long synthesis or a
        # multi-claim decomposition JSON mid-object with no visible error.
        self.max_tokens = max_tokens

        # 7. Provider-specific request fields merged into every payload --
        # e.g. NVIDIA's chat_template_kwargs to switch a reasoning model's
        # thinking off (see config.build_llm_clients).
        self.extra_body = dict(extra_body) if extra_body else {}

    def complete(self, prompt: str, system: Optional[str] = None, temperature: float = 0.2) -> str:
        messages = []

        # 1. ONLY add the system prompt if it exists
        if system:
            messages.append({"role": "system", "content": system})

        # 2. ALWAYS add the user's prompt (notice this is OUTSIDE the `if` block)
        messages.append({"role": "user", "content": prompt})

        # 3. Hand the list to our engine, and return whatever the engine gives back
        return self._chat(messages, temperature=temperature)

    def complete_json(self, prompt: str, system: Optional[str] = None, temperature: float = 0.1) -> Any:
        # 1. Force the strict JSON rule
        # If there is a system prompt, we add two newlines after it. If not, we start blank.
        base_system = system + "\n\n" if system else ""

        # We append our strict instructions to make sure the AI knows exactly what to do.
        json_system = (
            base_system +
            "Respond with ONLY valid JSON. No markdown code fences, no "
            "explanation, no preamble -- the entire response must be a single "
            "parseable JSON value."
        )

        # 2. Call the engine you built (notice the lower temperature for more predictable output)
        raw = self.complete(prompt, system=json_system, temperature=temperature)

        # 3. Clean the text using our static helper method
        cleaned = self._strip_json_fences(raw)

        # 4. Safely parse it into a Python dictionary
        try:
            return json.loads(cleaned)

        # 5. Catch the specific JSON failure -- but first try a conservative
        # repair. Seen live from the Nemotron verifier, on most claims:
        #     "reason": The evidence directly states ...
        # (one bare, unquoted string value in otherwise valid JSON). Without
        # the repair every such answer was thrown away and re-asked on the
        # next failover key, multiplying a single Clinical Evidence question
        # into 10+ minutes of retries.
        except json.JSONDecodeError as exc:
            repaired = self._repair_json(cleaned)
            if repaired is not None:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    pass
            # We slice raw[:500] so if the AI went crazy and wrote a 10-page essay,
            # we only print the first 500 characters to our error logs.
            raise LLMerror(f"LLM did not return valid JSON. Raw response:\n{raw[:500]}") from exc

    # A line holding `"key": <value>` whose value is not already a JSON
    # string/number/literal/object/array -- i.e. a bare sentence.
    _BARE_VALUE_LINE = re.compile(
        # (?=\S) pins the check to the value's first real character -- without
        # it the regex backtracks into the whitespace and "re-quotes" values
        # that were already valid.
        r'^(\s*"[^"\n]+"\s*:[ \t]*)(?=\S)(?!["\[{]|-?\d|true\b|false\b|null\b)(.*?)(\s*,)?\s*$'
    )

    @classmethod
    def _repair_json(cls, text: str) -> Optional[str]:
        """Best-effort fixes for the near-JSON models actually emit: prose
        around the value, bare unquoted string values (one per line), and
        trailing commas. Returns None when there's nothing JSON-shaped to
        repair. Never invents keys or values -- it only re-quotes and trims
        what the model already wrote, so a repaired answer still has to pass
        every downstream validator."""
        starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
        if not starts:
            return None
        start = min(starts)
        end = max(text.rfind("}"), text.rfind("]"))
        if end <= start:
            return None
        body = text[start:end + 1]

        lines = []
        for line in body.split("\n"):
            m = cls._BARE_VALUE_LINE.match(line)
            if m and m.group(2):
                line = f"{m.group(1)}{json.dumps(m.group(2).strip())}{m.group(3) or ''}"
            lines.append(line)
        body = re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))
        try:
            json.loads(body)
            return body
        except json.JSONDecodeError:
            pass
        # Same defect on ONE line (also seen live):
        #   {"verdict": "SUPPORTS", ..., "reason": The evidence states X.}
        return re.sub(r",(\s*[}\]])", r"\1", cls._quote_inline_bare_values(body))

    _KEY_PREFIX = re.compile(r'"[^"\n]+"\s*:[ \t]*')
    _NEXT_KEY = re.compile(r',\s*"[^"\n]+"\s*:')
    _JSON_VALUE_START = re.compile(r'["\[{]|-?\d|true\b|false\b|null\b')

    @classmethod
    def _quote_inline_bare_values(cls, body: str) -> str:
        """Quote a bare value that runs until the next `, "key":` or the
        closing brace of its object, whichever comes first."""
        pos = 0
        while True:
            m = cls._KEY_PREFIX.search(body, pos)
            if not m:
                return body
            start = m.end()
            if start >= len(body) or cls._JSON_VALUE_START.match(body, start):
                pos = start
                continue
            next_key = cls._NEXT_KEY.search(body, start)
            close = body.find("}", start)
            ends = [i for i in (next_key.start() if next_key else -1, close) if i != -1]
            if not ends:
                return body
            end = min(ends)
            value = body[start:end].strip()
            if not value:
                pos = start
                continue
            quoted = json.dumps(value)
            body = body[:start] + quoted + body[end:]
            pos = start + len(quoted)

    @staticmethod
    def _strip_json_fences(text: str) -> str:
        """
        FIX: this method was CALLED in complete_json() but never DEFINED anywhere
        in the file -- that's the crash you'd have hit first. This is the piece
        that strips ```json ... ``` or ``` ... ``` wrapping that models add even
        when told not to, so json.loads() gets clean text instead of markdown.
        """
        text = text.strip()
        fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()
        return text

    def _headers(self) -> dict:
        # 1. Start with the default headers we always need
        headers = {"Content-Type": "application/json"}

        # 2. In Python, `if self.api_key:` is a clean shorthand for `if self.api_key != None:`
        if self.api_key:
            # 3. Use standard "Authorization" key and correct f-string syntax
            headers["Authorization"] = f"Bearer {self.api_key}"

        # 4. Hand the dictionary back to whatever called this method
        return headers

    def _chat(self, messages: list[dict], temperature: float = 0.2) -> str:
        # 1. Build the Payload (the actual data we are sending)
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.extra_body:
            payload.update(self.extra_body)

        # 2. The Try Block: Attempting the risky internet connection
        try:
            # We use our session to make a POST request to the server
            resp = self.session.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.timeout,  # FIX: was self.time_out (AttributeError) -- now matches __init__
            )
            # This is a magic method in the `requests` library.
            # If the server returned an error code (like 401 Unauthorized or 500 Server Error),
            # this line will immediately trigger an exception.
            resp.raise_for_status()

        # 3. The Except Block: Catching the failure
        except requests.RequestException as exc:
            # We catch the generic `requests` error, and raise our CUSTOM error.
            # The `from exc` part is a Python trick that preserves the original error trace
            # so we can see exactly what went wrong if we are debugging later.
            raise LLMerror(f"LLM request failed: {exc}") from exc

        # 1. Convert the raw response into a Python dictionary
        data = resp.json()

        # 1b. A response cut off by the token budget is NOT the same as a
        # clean completion -- it can leave a caller holding a half-written
        # answer (or, for a reasoning model whose chain-of-thought lands in
        # this same `content` field, a response that never got past
        # "thinking" to write the actual answer at all). Surface this in
        # the logs; the caller still gets whatever text came back, since a
        # truncated response is sometimes still partially usable and the
        # existing deterministic parsers (citation-tag parsing, JSON
        # parsing) already reject what they can't use.
        try:
            finish_reason = data["choices"][0].get("finish_reason")
        except (KeyError, IndexError):
            finish_reason = None
        if finish_reason == "length":
            print(
                f"[core.llm] response truncated by max_tokens ({self.max_tokens}); "
                "the model may not have finished reasoning before hitting the limit"
            )

        # 2. Try to extract the text
        try:
            return data["choices"][0]["message"]["content"]

        # 3. The Required Partner: What if the API changes and "choices" doesn't exist?
        except (KeyError, IndexError) as exc:
            # KeyError catches a missing dictionary key (like if "message" is missing)
            # IndexError catches an empty list (like if "choices" has 0 items)
            raise LLMerror(f"Unexpected LLM response shape: {data}") from exc


# FIX: agents/appraiser.py does `from core.llm import LLMClient, LLMError` (capital C, capital E).
# Your class names here are LLMclient / LLMerror. Rather than making you go edit every
# other file that imports this one, these aliases make both spellings work.
LLMClient = LLMclient
LLMError = LLMerror