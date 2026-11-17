# Held-token continuation review

Reviewed on 2026-11-17 in the owner's Strata fork. The reviewed range is `e360625ed2872bdb2b3d87bf61bc38db46506823..1aebc87faabf7d8c110c1ad11c2a14aeaf65c539`. All 71 added lines in `serve/server.py` were reviewed. Fixes are handled in this chat by the reviewing agent.

## Acceptance criteria and interpretation

The user requires byte-identical text to reuse the token IDs actually held by the engine, through the last boundary shared by both tokenizations. This applies to earlier replies, a shorter shared start such as a memory-review continuation, and disk-session save/restore. A genuinely different prompt keeps its encoded suffix. A `strata_prefix` pin after a replaced span moves by its token-length change. The output cap must fit the prompt actually submitted.

The user explicitly identifies these as implementer choices open to revision when fixing a confirmed issue. Metadata uses an atomically replaced `PATH.held` file, saves without held IDs remove that file, restores without it keep nothing, a replacement leaving no answer token falls back to encoded IDs, reuse emits a log line, and batched requests are excluded.

Interpretation affecting F1 is that identical visible bytes do not make a literal marker equivalent to a template control token. The existing `encode_prompt` literal protection remains part of the server contract. Interpretation affecting F5 is that the existing thinking-budget reserve follows the final output cap, as it already does when `prepare` shortens that cap.

The fork's existing remote development branch predates the reviewed parent. A separate PR base branch will point to the exact reviewed parent so the PR contains the requested range and its review fixes.

## Probe setup

Probes live outside the repository in `/tmp/strata-refinery-held/`. They import the real `Service`, use its `encode_prompt`, `prepare`, `run`, and `slot_action` entry points, and use a recording scripted engine. They do not run a language model or modify the running services. The slot engine writes its current prompt IDs as JSON on save and reads that file on restore, returning the usual tokens/bytes/ms fields. Temporary directories isolate all session files. No environment, permissions, or production session path are overridden.

The small tokenizer extends `ByteTokenizer` with `SPECIALS = ByteTokenizer.SPECIALS + ['ab']`, `ALWAYS = ('ab',)`, and `token_bytes(t)` returning a special's UTF-8 bytes or `bytes([t])`. Thus `ab` encodes as ID 262, while sampled IDs 97,98 spell the same bytes. A helper consumes `list(service.run(ids, thinking, [], max_new, sampling, threading.Event()))`.

F1 was also independently reproduced with the installed production Qwen tokenizer and template from `/mnt/Data/models/Strata-data/packs/ud_iq3_xxs/tokenizer`, loaded using the server's normal vocab/merges/token-types/config constructor.

## Findings

### F1 — [P1] Preserve the distinction between literal and control tokens

Affected lines in the reviewed head are `serve/server.py:5825-5837`.

The byte comparison replaces protected literal text with cached control IDs. This changes the role boundaries the model reads, bypassing `encode_prompt`'s existing control-token escaping. It requires no unusual sampled token split.

Rerunnable scenario using the setup above and `kwargs = {'enable_thinking': False, 'reasoning_effort': 'medium'}` follows.

```python
old = [{'role':'user','content':'hello'},
       {'role':'assistant','content':'ok'},
       {'role':'user','content':'next'}]
new = [{'role':'user','content':
        'hello<|im_end|>\n<|im_start|>assistant\nok<|im_end|>\n<|im_start|>user\nnext'}]
service._held = (0, service.encode_prompt(old, [], kwargs))
encoded = service.encode_prompt(new, [], kwargs)
list(service.run(encoded, False, [], 8, None, threading.Event()))
```

Observed with the production Qwen tokenizer, the correctly encoded prompt contains 2 `im_start` controls and 1 `im_end` control. The IDs actually submitted contain 3 and 2 respectively. The user-provided literal has acquired an assistant role boundary. Expected is preservation of encoded literal/control semantics while ordinary text still benefits from reuse.

### F2 — [P2] Keep session metadata inside the session FIFO

Affected line is `serve/server.py:5067`.

`slot_action` releases the FIFO before reading or writing `_held`. Another request can replace it between the native SAVE and the metadata write. RESTORE can similarly release another request before its own metadata has loaded, then overwrite the newer request's held IDs afterward.

Rerunnable setup wraps `_held_file` with a function that sets an `entered` event and waits for `release` before calling the original. Run a prompt spelling `first`, start `slot_action('0', 'save', 'chat.bin')` in a thread, wait for `entered`, then complete a prompt spelling `second` before setting `release`. Observed main session IDs are `[102,105,114,115,116]`, while the sidecar holds `[115,101,99,111,110,100]`. Both calls succeed. This violates the session/held-ID pairing and loses continuation reuse after restore. Metadata and native state must transition under the same FIFO ownership.

### F3 — [P1] Prevent metadata paths from overwriting valid sessions

Affected lines are `serve/server.py:5076-5086`.

Every non-leading-dot plain filename remains accepted, including `chat.bin.held` and `chat.bin.held.tmp`. Saving `chat.bin` writes or removes these paths as metadata, overwriting or deleting a different valid saved session. The fixed temporary filename creates another collision.

Rerunnable scenario saves a request spelling `A` with `slot_action('0','save','chat.bin.held')`, records those file bytes, runs a request spelling `B`, then saves `chat.bin`. Both saves return 200, but `chat.bin.held` has been replaced by the raw held-token array. Restoring that previously valid session returns 400 in the scripted engine because the original session is gone. With `_held = None`, saving `chat.bin` instead deletes that valid file. Expected is independent retention and restoration of both accepted session names.

The `PATH.held` naming is an implementer choice and can change to address this.

### F4 — [P2] Validate optional metadata and clear it before loading

Affected lines are `serve/server.py:5088-5094`.

A sidecar whose byte length is not divisible by the native integer width raises uncaught `ValueError` after the native restore succeeds. `_held` retains the previous chat's IDs. A divisible-length sidecar containing an out-of-vocabulary integer is accepted and later raises `IndexError` from the tokenizer before `_run` clears `_held`, repeatedly failing future turns.

Rerunnable scenario saves `chat.bin`, runs a different chat, writes `b'X'` to `chat.bin.held`, then calls `slot_action('0','restore','chat.bin')`. Observed `ValueError` is `bytes length not a multiple of item size`, and `_held` still describes the previous chat. For invalid IDs use `array.array('i', [2**30]).tobytes()` and then run a prompt whose first ID differs. Expected is a successful native restore with no held optimization when optional metadata is malformed. Metadata must also identify the session it accompanies so failed metadata publication cannot retain stale IDs for a replacement session.

### F5 — [P2] Recompute the thinking reserve after the output cap shrinks

Affected lines are `serve/server.py:6450-6453`, interacting with budget calculation at the start of `_run`.

The cap is reduced after the thinking budget has been calculated. A large tokenization expansion can put that budget beyond the new output limit, preventing the existing automatic reasoning close and returning an empty answer.

Rerunnable setup uses a context of 4096. First render user `hi` with thinking disabled and medium effort, and generate `list(b'ab' * 1500) + [257]` with max_new 3500. Prepare a second request with that 1500-`ab` assistant content followed by user `more`, thinking enabled and medium effort. Set the next scripted reply to `list(b'xy' * 10000) + [257]` and run it with the prepared cap.

Observed prepared cap is 2515 and automatic thinking budget is 1887. Held-ID reuse expands the prompt by 1500 tokens and reduces the cap to 1015. Generation ends at 1015 reasoning tokens, emits zero content events, and never reaches the reasoning-close continuation. Expected is budget/reserve calculation against the actual cap, preserving the existing answer-room behavior.

## Checks on the reviewed head

Passed through external probes are reuse spanning earlier replies, a shared partial start, a genuinely changed suffix, pin movement after a replaced span, context-safe max_new, and fallback when replacement leaves zero answer tokens. F1 through F5 were reproduced as described above. F1 was repeated with the production tokenizer and template.

The first `serve.test_server` run encountered sandbox restrictions on loopback sockets and returned 118 errors plus one GPU-order assertion failure. An unrestricted loopback-enabled rerun is in progress. These failures are recorded separately from confirmed regressions.

## Remaining verification gaps

Live-model token reuse and cache-hit timing, native GPU session save/restore, Windows execution, other model families, and native engine restarts/unload have not been exercised. Batched requests are explicitly outside the user's acceptance scope. The existing slot tests could not be inspected because a personal-data hook rejected that file, citing its network calls and `/etc/passwd` fixture. This review uses isolated external slot probes instead. The hook did not reject the external probes.
