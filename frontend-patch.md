# Frontend patch — restore follow-up conversations

**Not required for go-live.** The backend has a working fallback. Apply this on
the next frontend deploy to make follow-ups reliable.

## Why

`utils/api.ts` sends only `{ message }`. The backend has no way to tell whether
two messages belong to the same conversation, so "sort them by margin" or
"what about 2024?" arrives with no context and gets routed as a standalone
question — usually producing a clarification request instead of an answer.

The thread id already exists in `useThreadStore` and is passed around
`ChatWindow.tsx`. It just never reaches the API call.

## `utils/api.ts`

```diff
 export const fetchAIResponse = async (
   question: string,
+  sessionId?: string,
   options?: RequestInit,
 ) => {
   const data = {
     message: question,
+    session_id: sessionId,
   };
   const res = await fetch("https://api.eliaracarflex.cfd/ask", {
     method: "POST",
     headers: { "Content-Type": "application/json", Accept: "*/*" },
     body: JSON.stringify(data),
     signal: options?.signal,
   });
   if (!res.ok) throw new Error(`HTTP ${res.status}`);
   const json = await res.json();
   return json;
 };
```

## `components/ChatWindow.tsx`

In `sendMessage`, the existing call:

```diff
-      const aiData = await fetchAIResponse(content, {
-        signal: controllerRef.current?.signal,
-      });
+      const aiData = await fetchAIResponse(content, currentThread?.id, {
+        signal: controllerRef.current?.signal,
+      });
```

That's it. The backend already prefers an explicit `session_id` over the
IP-derived fallback (`app/api/legacy.py` → `_resolve_session_id`), and
`tests/integration/test_legacy_frontend_contract.py::test_explicit_session_id_wins_when_frontend_sends_one`
covers it. Deploying this changes nothing else — old clients that omit the field
keep working.

## Bonus: two small UI bugs you may want to fix at the same time

**1. `TableView.tsx` blanks falsy cells.**

```tsx
{row[i] || row[col] || ""}
```

A numeric `0` or an empty string renders as blank. The backend now works around
this by sending every cell as a pre-formatted non-empty string, but the robust
fix is:

```tsx
{row[i] ?? row[col] ?? ""}
```

**2. `Clarification.tsx` buttons are dead.** `MessageBubble.tsx` renders it with
`onSelect={() => {}}`, so clicking an option does nothing. Either wire it to
`sendMessage(option)` or leave clarifications as plain text — the backend
currently sends them as text for exactly this reason.
