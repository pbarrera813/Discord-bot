from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

import aiohttp


class XAIClient:
    BASE_URL = "https://api.x.ai/v1/responses"

    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model
        self._timeout = aiohttp.ClientTimeout(total=120)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def chat(
        self,
        *,
        server_context: str,
        user_message: str,
        author_name: str,
        channel_name: str,
        channel_reference: str | None = None,
        available_channels: list[str] | None = None,
        available_emojis: list[str] | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        mention_hints: list[str] | None = None,
        relay_instruction: str | None = None,
    ) -> str:
        context_suffix = self._build_context_suffix(
            server_context=server_context,
            user_message=user_message,
            conversation_history=conversation_history,
        )
        channels_suffix = ""
        if available_channels:
            channels_preview = "\n".join(f"- {item}" for item in available_channels[:60])
            channels_suffix = (
                "\nAvailable text channels (use these for references and mentions):\n"
                f"{channels_preview}"
            )
        emojis_suffix = ""
        if available_emojis:
            emojis_preview = "\n".join(f"- {item}" for item in available_emojis[:80])
            emojis_suffix = (
                "\nAvailable custom server emojis (use the exact token when needed):\n"
                f"{emojis_preview}"
            )
        mention_hints_suffix = ""
        if mention_hints:
            mention_hints_preview = "\n".join(f"- {item}" for item in mention_hints[:20])
            mention_hints_suffix = (
                "\nUser mention hints for current message (prefer these mappings):\n"
                f"{mention_hints_preview}"
            )
        relay_suffix = ""
        if relay_instruction:
            relay_suffix = f"\nRelay handling instruction:\n{relay_instruction.strip()}"

        system_prompt = (
            "You are Nitori Kawashiro from Touhou Project, speaking in first person as the bot itself. "
            "If users say 'Nitori' or similar nicknames, treat it as them talking to you directly, not to a third person. "
            "Act as a lively Discord community member first, not as a programming assistant by default. "
            "Use natural banter, teasing, and light-to-moderate sarcasm when it fits the server vibe. "
            "If users use stronger language or dark humor as jokes, stay in-character and reply naturally instead of generic refusal lines when safe. "
            "Mild-to-strong profanity is allowed when it matches the conversation tone. "
            "For ordinary banter, roasts, and edgy jokes, avoid moralizing lectures and keep the flow natural. "
            "Only refuse requests that are clearly illegal or exploitative in the real world. "
            "Follow the server context and the user's tone/humor for each reply. "
            "Inside jokes from server context are optional: only use one when it is directly relevant to the user's current message or recent topic. "
            "If not relevant, skip inside jokes entirely instead of forcing them. "
            "Avoid random inside-joke drops that break coherence. "
            "Continue the current conversation naturally. Do not act like every message is a brand-new chat. "
            "For normal chat, never use generic refusal lines; always give an in-character response. "
            "If asked to do something you cannot directly execute in Discord, still try: provide the best practical workaround. "
            "Avoid policy/meta disclaimers and canned responses unless a request is truly unsafe. "
            "Only switch to technical/coding helper mode when the user clearly asks for coding help. "
            "You do have access to the channel list provided in this prompt. "
            "Never claim you cannot access channels or channel IDs. "
            "When mentioning users/channels, prefer Discord mention format (<@id> or <#id>) if available. "
            "For custom emojis, prefer valid custom emoji tokens like <:name:id> or <a:name:id> from the provided list. "
            "If a user asks for a channel, pick the best match from the provided channel list and mention it directly. "
            "If there is a relay/reminder request to another user, mention the resolved target user, "
            "and do not ping the requester unless explicitly asked. "
            "Do not repeat the same channel mention twice in one sentence unless the user explicitly asks you to repeat it. "
            "If the answer is long, finish your full thought and avoid cutting the message abruptly. "
            f"You are speaking in channel '{channel_name}'"
            + (f" ({channel_reference})" if channel_reference else "")
            + f".{context_suffix}{channels_suffix}{emojis_suffix}{mention_hints_suffix}{relay_suffix}"
        )

        messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            for item in conversation_history[-28:]:
                role = item.get("role", "").strip().lower()
                content = str(item.get("content", "")).strip()
                if role not in {"user", "assistant"} or not content:
                    continue
                messages.append({"role": role, "content": content[:1400]})
        messages.append(
            {
                "role": "user",
                "content": f"{author_name}: {user_message}",
            }
        )

        initial = await self._create_completion_with_retry(
            messages,
            temperature=0.8,
            max_tokens=1100,
            retries=2,
            retry_on_refusal=True,
            user_message_for_fallback=user_message,
        )
        if not self._looks_incomplete_response(initial):
            return initial

        continuation_messages = list(messages)
        continuation_messages.append({"role": "assistant", "content": initial[-1800:]})
        continuation_messages.append(
            {
                "role": "user",
                "content": (
                    "Continue exactly from where your previous message stopped. "
                    "Do not repeat previous text, do not add headers like 'continuing', "
                    "and finish the thought naturally."
                ),
            }
        )
        continuation = await self._create_completion_with_retry(
            continuation_messages,
            temperature=0.7,
            max_tokens=700,
            retries=1,
            retry_on_refusal=False,
        )
        if not continuation.strip():
            return initial
        return self._merge_continuation(initial, continuation)

    def _build_context_suffix(
        self,
        *,
        server_context: str,
        user_message: str,
        conversation_history: list[dict[str, str]] | None,
    ) -> str:
        raw = server_context.strip()
        if not raw:
            return ""

        sections = self._parse_server_context(raw)
        tone = sections.get("tone", "")
        jokes = sections.get("inside_jokes", "")
        topics = sections.get("common_topics", "")
        personality = sections.get("personality_style", "")
        reply_style = sections.get("reply_style", "")

        relevant_jokes = self._select_relevant_inside_jokes(
            jokes,
            user_message=user_message,
            conversation_history=conversation_history,
        )

        lines: list[str] = []
        if tone:
            lines.append(f"Tone: {self._clip(tone, 260)}")
        if topics:
            lines.append(f"Common topics: {self._clip(topics, 260)}")
        if personality:
            lines.append(f"Personality style: {self._clip(personality, 260)}")
        if reply_style:
            lines.append(f"How the bot should reply: {self._clip(reply_style, 320)}")

        if relevant_jokes:
            rendered = " | ".join(self._clip(item, 110) for item in relevant_jokes[:2])
            lines.append(
                "Relevant inside jokes for this reply (optional): "
                f"{rendered}"
            )
        else:
            lines.append(
                "Relevant inside jokes for this reply: none selected. "
                "Do not force any inside joke."
            )

        if not lines:
            return f"\nServer context provided by admins:\n{self._clip(raw, 700)}"
        return "\nServer context provided by admins:\n" + "\n".join(lines)

    @staticmethod
    def _clip(value: str, limit: int) -> str:
        text = " ".join(value.strip().split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _parse_server_context(self, raw: str) -> dict[str, str]:
        result = {
            "tone": "",
            "inside_jokes": "",
            "common_topics": "",
            "personality_style": "",
            "reply_style": "",
        }

        label_map = {
            "tone": "tone",
            "tono": "tone",
            "inside jokes": "inside_jokes",
            "inside jokes/memes": "inside_jokes",
            "inside jokes memes": "inside_jokes",
            "memes": "inside_jokes",
            "chistes internos": "inside_jokes",
            "common topics": "common_topics",
            "temas comunes": "common_topics",
            "personality style": "personality_style",
            "estilo de personalidad": "personality_style",
            "how the bot should reply": "reply_style",
            "como debe responder el bot": "reply_style",
            "como deberia responder el bot": "reply_style",
        }
        for line in raw.splitlines():
            chunk = line.strip()
            if not chunk:
                continue
            if ":" not in chunk:
                continue
            key, value = chunk.split(":", 1)
            normalized_key = self._normalize_lookup(key)
            target = label_map.get(normalized_key)
            if not target:
                continue
            value_clean = value.strip()
            if not value_clean:
                continue
            if result[target]:
                existing_norm = self._normalize_lookup(result[target])
                incoming_norm = self._normalize_lookup(value_clean)
                if incoming_norm not in existing_norm:
                    result[target] = f"{result[target]} | {value_clean}"
            else:
                result[target] = value_clean
        if any(result.values()):
            return result

        pieces = [part.strip() for part in raw.split(";") if part.strip()]
        if len(pieces) >= 5:
            result["tone"] = pieces[0]
            result["inside_jokes"] = pieces[1]
            result["common_topics"] = pieces[2]
            result["personality_style"] = pieces[3]
            result["reply_style"] = pieces[4]
            return result

        # Fallback for legacy freeform contexts.
        result["reply_style"] = raw
        return result

    def _select_relevant_inside_jokes(
        self,
        jokes_text: str,
        *,
        user_message: str,
        conversation_history: list[dict[str, str]] | None,
    ) -> list[str]:
        candidates = self._extract_inside_joke_candidates(jokes_text)
        if not candidates:
            return []

        focus_text = " ".join(
            [
                user_message.strip(),
                self._recent_user_focus_text(conversation_history),
            ]
        ).strip()
        focus_tokens = self._tokenize(focus_text)
        if not focus_tokens:
            return []

        recent_assistant = self._recent_assistant_text(conversation_history)
        scored: list[tuple[int, str]] = []
        for candidate in candidates:
            normalized_candidate = self._normalize_lookup(candidate)
            if normalized_candidate and normalized_candidate in recent_assistant:
                continue
            candidate_tokens = self._tokenize(candidate)
            if not candidate_tokens:
                continue
            overlap = len(focus_tokens.intersection(candidate_tokens))
            if overlap <= 0:
                continue
            score = overlap * 10 + min(len(candidate_tokens), 8)
            scored.append((score, candidate))

        if not scored:
            return []
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:2]]

    @staticmethod
    def _extract_inside_joke_candidates(text: str) -> list[str]:
        raw = text.strip()
        if not raw:
            return []
        parts = re.split(r"[|\n;]", raw)
        cleaned: list[str] = []
        seen: set[str] = set()
        for part in parts:
            candidate = part.strip(" -*\t")
            candidate = re.sub(r"^\d+[.)]\s*", "", candidate).strip()
            if len(candidate) < 4:
                continue
            key = candidate.casefold()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(candidate)
        return cleaned

    def _recent_user_focus_text(self, conversation_history: list[dict[str, str]] | None) -> str:
        if not conversation_history:
            return ""
        snippets: list[str] = []
        for item in reversed(conversation_history[-20:]):
            if str(item.get("role", "")).strip().lower() != "user":
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            snippets.append(content)
            if len(snippets) >= 4:
                break
        snippets.reverse()
        return " ".join(snippets)

    def _recent_assistant_text(self, conversation_history: list[dict[str, str]] | None) -> str:
        if not conversation_history:
            return ""
        snippets: list[str] = []
        for item in conversation_history[-16:]:
            if str(item.get("role", "")).strip().lower() != "assistant":
                continue
            content = str(item.get("content", "")).strip()
            if content:
                snippets.append(content)
        return self._normalize_lookup(" ".join(snippets))

    def _normalize_lookup(self, text: str) -> str:
        folded = text.casefold().strip()
        normalized = unicodedata.normalize("NFKD", folded)
        return "".join(ch for ch in normalized if not unicodedata.combining(ch))

    def _tokenize(self, text: str) -> set[str]:
        normalized = self._normalize_lookup(text)
        return {
            token
            for token in re.findall(r"[a-z0-9_]{3,}", normalized)
            if token not in {"the", "and", "for", "with", "that", "this", "como", "para", "con", "que"}
        }

    async def translate(self, *, text: str, target_language: str) -> str:
        messages = [
            {
                "role": "system",
                "content": (
                    "Translate the user text into the requested language. "
                    "Return only the translation text without explanations."
                ),
            },
            {
                "role": "user",
                "content": f"Target language: {target_language}\nText: {text}",
            },
        ]
        return await self._create_completion_with_retry(
            messages,
            temperature=0.2,
            max_tokens=500,
            retries=1,
            retry_on_refusal=False,
        )

    async def summarize_server_context(
        self, *, channel_name: str, messages_transcript: str, language: str
    ) -> str:
        language_name = "Spanish" if language == "es" else "English"
        trimmed = messages_transcript[:10000]
        messages = self._summary_messages(channel_name, trimmed, language_name, compact=False)

        try:
            return await self._create_completion(messages, temperature=0.3, max_tokens=700)
        except RuntimeError as exc:
            if "empty completion" not in str(exc).lower():
                raise

            compact_trimmed = messages_transcript[:4500]
            retry_messages = self._summary_messages(
                channel_name, compact_trimmed, language_name, compact=True
            )
            try:
                return await self._create_completion(
                    retry_messages,
                    temperature=0.2,
                    max_tokens=480,
                )
            except RuntimeError:
                return self._fallback_summary(channel_name, language)

    @staticmethod
    def _summary_messages(
        channel_name: str, transcript: str, language_name: str, *, compact: bool
    ) -> list[dict[str, str]]:
        detail_line = (
            "Keep it under 800 characters and make it highly practical."
            if compact
            else "Keep it under 1400 characters."
        )
        return [
            {
                "role": "system",
                "content": (
                    "You summarize Discord server culture for a bot system prompt. "
                    "Return concise plain text focused on tone, humor style, slang, inside jokes, repeated references, "
                    "common topics, personality dynamics, and response style preferences. "
                    "Use exactly five lines with this structure:\n"
                    "Tone: ...\n"
                    "Inside jokes/memes: ...\n"
                    "Common topics: ...\n"
                    "Personality style: ...\n"
                    "How the bot should reply: ...\n"
                    "Do not force jokes: include only jokes/memes that are commonly reused and explain when they apply. "
                    f"Write the summary in {language_name}. {detail_line}"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Channel: #{channel_name}\n"
                    "Messages from last week:\n"
                    f"{transcript}"
                ),
            },
        ]

    @staticmethod
    def _fallback_summary(channel_name: str, language: str) -> str:
        if language == "es":
            return (
                f"Resumen base de #{channel_name}: tono casual y comunitario. "
                "Responde de forma breve, natural y amable; usa humor cuando encaje. "
                "Mantiene estilo claro y conversacional."
            )
        return (
            f"Baseline summary from #{channel_name}: casual community tone. "
            "Respond briefly, naturally, and friendly; use humor when appropriate. "
            "Keep a clear conversational style."
        )

    async def _create_completion(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> str:
        session = await self._get_session()
        payload: dict[str, Any] = {
            "model": self.model,
            "input": messages,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with session.post(self.BASE_URL, json=payload, headers=headers) as resp:
            raw_text = await resp.text()
            try:
                data: dict[str, Any] = json.loads(raw_text) if raw_text else {}
            except json.JSONDecodeError:
                data = {}

            if resp.status >= 400:
                message = self._extract_error_message(data, raw_text)
                raise RuntimeError(f"xAI API error ({resp.status}): {message}")

            content = self._extract_output_text(data)
            if not content:
                raise RuntimeError("xAI API returned an empty completion.")
            return content.strip()

    @staticmethod
    def _extract_error_message(data: dict[str, Any], raw_text: str) -> str:
        error_obj = data.get("error")
        if isinstance(error_obj, dict):
            msg = error_obj.get("message")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
            code = error_obj.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
        elif isinstance(error_obj, str) and error_obj.strip():
            return error_obj.strip()

        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()

        trimmed = raw_text.strip()
        if trimmed:
            if len(trimmed) > 220:
                return f"{trimmed[:217]}..."
            return trimmed
        return "Unknown xAI API error"

    @staticmethod
    def _extract_output_text(data: dict[str, Any]) -> str:
        output_text = data.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text

        chunks: list[str] = []
        output_items = data.get("output")
        if isinstance(output_items, list):
            for item in output_items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", ""))
                if item_type == "message":
                    content_parts = item.get("content")
                    if isinstance(content_parts, list):
                        for part in content_parts:
                            if not isinstance(part, dict):
                                continue
                            text = part.get("text")
                            if isinstance(text, str) and text:
                                chunks.append(text)
                elif item_type in {"output_text", "text"}:
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        chunks.append(text)

        if chunks:
            return "".join(chunks)

        # Compatibility fallback if a provider returns chat-completions style payload.
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content") if isinstance(message, dict) else ""
            if isinstance(content, str) and content.strip():
                return content

        return ""

    async def _create_completion_with_retry(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
        retries: int,
        retry_on_refusal: bool,
        user_message_for_fallback: str | None = None,
    ) -> str:
        last_error: RuntimeError | None = None
        retry_messages = list(messages)
        for attempt in range(retries + 1):
            try:
                content = await self._create_completion(
                    retry_messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                )
                if retry_on_refusal and self._looks_like_refusal(content) and attempt < retries:
                    retry_messages = list(messages)
                    retry_messages.append(
                        {
                            "role": "system",
                            "content": (
                                "Retry with a natural in-character reply for normal banter. "
                                "Do not use generic refusal lines for jokes/insults/dark humor. "
                                "Only refuse if the request is clearly illegal or exploitative."
                            ),
                        }
                    )
                    continue
                if retry_on_refusal and self._looks_like_refusal(content):
                    candidate = user_message_for_fallback or ""
                    if candidate and not self._looks_illegal_or_exploitative(candidate):
                        return self._fallback_banter_reply(candidate)
                return content
            except RuntimeError as exc:
                last_error = exc
                if attempt < retries and self._is_retryable_completion_error(exc):
                    continue
                if retry_on_refusal and user_message_for_fallback:
                    if not self._looks_illegal_or_exploitative(user_message_for_fallback):
                        return self._fallback_banter_reply(user_message_for_fallback)
                raise

        if last_error is not None:
            if retry_on_refusal and user_message_for_fallback:
                if not self._looks_illegal_or_exploitative(user_message_for_fallback):
                    return self._fallback_banter_reply(user_message_for_fallback)
            raise last_error
        raise RuntimeError("xAI API returned an empty completion.")

    @staticmethod
    def _is_retryable_completion_error(error: RuntimeError) -> bool:
        message = str(error).lower()
        retryable_markers = (
            "empty completion",
            "no completion choices",
            "refusal without completion",
            "timed out",
            "timeout",
            "502",
            "503",
            "504",
        )
        return any(marker in message for marker in retryable_markers)

    @staticmethod
    def _looks_like_refusal(content: str) -> bool:
        lowered = content.lower().strip()
        refusal_markers = (
            "i can't help with that",
            "i cannot help with that",
            "i can't assist with that",
            "i cannot assist with that",
            "i'm not able to help with that",
            "i can't comply with that",
            "i cannot comply with that",
            "i'm sorry, but i can't",
            "i'm sorry but i can't",
            "i cannot provide that",
            "i can't provide that",
            "i can't do that",
            "i cannot do that",
            "i won't help with that",
            "no puedo ayudar con eso",
            "no puedo ayudarte con eso",
            "no puedo asistir con eso",
            "no puedo cumplir con eso",
            "lo siento, no puedo",
            "no puedo hacer eso",
            "no voy a ayudarte con eso",
        )
        return any(marker in lowered for marker in refusal_markers)

    @staticmethod
    def _looks_illegal_or_exploitative(user_message: str) -> bool:
        lowered = user_message.casefold()
        markers = (
            "exploit",
            "zero-day",
            "malware",
            "ransomware",
            "phishing",
            "steal password",
            "stolen card",
            "credit card dump",
            "ddos",
            "sql injection",
            "hack account",
            "bypass 2fa",
            "fraud",
            "money laundering",
            "terrorist",
            "build a bomb",
            "csam",
            "child sexual abuse",
            "exploitar",
            "robar contrasena",
            "robar contraseña",
            "tarjetas robadas",
            "inyeccion sql",
            "inyección sql",
            "hackear cuenta",
            "fraude",
            "lavado de dinero",
            "terrorista",
            "fabricar una bomba",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _fallback_banter_reply(user_message: str) -> str:
        lowered = user_message.casefold()
        spanish_markers = (
            " que ",
            " por ",
            " porque",
            "jaja",
            "xd",
            "hola",
            "tu ",
            "tú ",
            "eres",
            "wey",
            "bro",
        )
        is_spanish = any(marker in lowered for marker in spanish_markers) or any(
            ch in user_message for ch in "áéíóúñ¿¡"
        )
        snippet = user_message.strip().replace("\n", " ")
        if len(snippet) > 160:
            snippet = f"{snippet[:157]}..."
        if is_spanish:
            return (
                f"Va, te sigo el juego. Sobre lo que dijiste ({snippet}), "
                "mi opinion: hay que ir con todo y mantener el ritmo del chat. "
                "Le meto humor y sarcasmo, evitando solo lo ilegal o explotativo."
            )
        return (
            f"Alright, I'm in. About what you said ({snippet}), "
            "my take is to lean into the bit and keep the convo moving. "
            "I'll match the humor/sarcasm and only avoid illegal or exploitative stuff."
        )

    @staticmethod
    def _looks_incomplete_response(content: str) -> bool:
        stripped = content.strip()
        if len(stripped) < 40:
            return False

        if stripped.endswith(("...", "…", "-", "—", ":", ";", ",", "(", "[", "{", "/", "\\")):
            return True

        if re.search(r"\b(?:si|if|pero|but|porque|because|cuando|when)\s*$", stripped, re.IGNORECASE):
            return True

        last_char = stripped[-1]
        if last_char in ".!?)]}\"'»":
            return False

        return bool(re.search(r"[A-Za-zÁÉÍÓÚáéíóúÑñ0-9]{3,}$", stripped))

    @staticmethod
    def _merge_continuation(initial: str, continuation: str) -> str:
        left = initial.rstrip()
        right = continuation.strip()
        if not left:
            return right
        if not right:
            return left

        max_overlap = min(160, len(left), len(right))
        overlap = 0
        left_fold = left.casefold()
        right_fold = right.casefold()
        for size in range(max_overlap, 8, -1):
            if left_fold[-size:] == right_fold[:size]:
                overlap = size
                break
        if overlap:
            right = right[overlap:].lstrip()
            if not right:
                return left

        if left[-1].isalnum() and right[0].isalnum():
            return left + right
        if left[-1] in "([{/" or right[0] in ".,;:!?)]}\"'»":
            return left + right
        return f"{left} {right}"
