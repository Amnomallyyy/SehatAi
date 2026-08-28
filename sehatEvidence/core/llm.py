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

        # 5. Catch the specific JSON failure
        except json.JSONDecodeError as exc:
            # We slice raw[:500] so if the AI went crazy and wrote a 10-page essay,
            # we only print the first 500 characters to our error logs.
            raise LLMerror(f"LLM did not return valid JSON. Raw response:\n{raw[:500]}") from exc

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