# Bounded-memory session restore review

Reviewed on 2026-11-16. Fixes in this PR target only the two findings below.

## Scope and interpretation

- Strata: `ef53559cec74cc4bddc78033f9fcc2ec94d2106e..7eb19f0829b62e36d7ef1ad67c69a30780b064bb` (`bba66bf5`, `7eb19f08`). All six changed files were reviewed.
- Dotware: `6698f1992c8464a33405248ebe6be273eec1f2e6`, all three changed files. No actionable finding in that configuration change.
- The supplied commit titles state the intent: opt-in bounded-memory restore, explicit initialization of optional streamed-file owners, and use of the local engine in Dotware. Interpreted requirements: preserve the default file format and restore behavior, validate the whole file before transfer, retain the opened file identity, recheck chunks before exposing bytes, preserve RAM admission, and keep Frost on its separate engine without the new flag. The 16 MiB chunk size and reader callback design are implementer choices.
- Uncommitted changes in `serve/frontend.py` and `serve/test_frontend.py` were excluded.
- This PR targets the reviewed local branch in the owner's fork; it does not submit the local server patches to upstream Strata.

## Findings

### F1 — [P2] Propagate a streamed buffer read failure when saving an image

Location introduced by this change: `src/core/conversation_file.cpp:785-801`; interacting caller: `Out::buffer` at lines 659-662 and `write_impl` at lines 1096-1109.

The new reader can return false before calling its visitor when the retained file is truncated, changed, or unreadable. The existing `Out::buffer` discards the return value of `b.visit`. It also reads the buffer during the sizing pass. If the reader fails in both passes, both calculate the same shortened payload, `out.n == payload` succeeds, and the writer publishes a file whose K/V length fields still describe the absent data. An existing valid destination is replaced and the API reports success.

Reproduced through `session_file_read(..., stream_kv=true)` and `session_file_write` on the reviewed head, with the normal default environment (no I/O override), using a temporary directory. Setup writes the existing `sample()` fixture to both source and destination, reads the source with streaming enabled, truncates that source to 64 bytes, then saves the returned image to the destination. Observed:

```text
write_ok=1 published=1 bytes=20479 error=
read_ok=0 error=session file: element count exceeds the payload
```

Expected: refuse the save and leave the valid destination intact. This affects the public image serialization API. The current engine SAVE command uses live `SessionKvSource` objects rather than reserializing the temporary restored image, so this probe does not establish corruption in the normal Dotware SAVE command.

Rerunnable probe (save outside the repository, compile from the reviewed checkout):

```cpp
#define main original_test_main
#include "src/core/conversation_file_test.cpp"
#undef main
int main() {
    const fs::path dir = fs::temp_directory_path() /
        ("strata-rewrite-probe-" + std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    fs::create_directories(dir);
    const auto src = (dir / "src.bin").string();
    const auto dst = (dir / "dst.bin").string();
    SessionFileIdentity id{1, 2};
    auto original = sample();
    std::string error;
    size_t bytes = 0;
    check(session_file_write(src, original, id, bytes, error), "setup write");
    check(session_file_write(dst, original, id, bytes, error), "setup destination");
    SessionReadLimits limits;
    limits.stream_kv = true;
    SavedConversation streamed;
    check(session_file_read(src, id, streamed, bytes, error, limits), "stream read");
    fs::resize_file(src, 64);
    SessionStatus status;
    const bool write_ok = session_file_write(dst, streamed, id, bytes, error, {}, &status);
    std::printf("write_ok=%d published=%d bytes=%zu error=%s\n", write_ok, status.published, bytes, error.c_str());
    SavedConversation reread;
    const bool read_ok = session_file_read(dst, id, reread, bytes, error);
    std::printf("read_ok=%d error=%s\n", read_ok, error.c_str());
}
```

```sh
g++ -std=c++20 -O2 -Wall -Wextra -Werror -I . -I include /tmp/rewrite_probe.cpp src/core/conversation_file.cpp -o /tmp/rewrite_probe
/tmp/rewrite_probe
```

### F2 — [P2] Make the streamed-file mutation tests respect Windows sharing

Location: `src/core/conversation_file_test.cpp:812-831`.

**Unconfirmed static analysis; not executed on Windows.** The new block is unconditional across platforms. `RawFile::open_session` on Windows opens with `FILE_SHARE_READ` only (`src/core/conversation_file.cpp:180-181`), and streamed buffers now keep that handle alive. The immediately following `fs::rename` at line 817 requires delete sharing, so it throws while `held` owns the handle. Even if that rename were bypassed, `spit(changed, damaged)` and `resize_file(changed, 64)` require write sharing and cannot mutate the held file. Thus the new standalone session-file test does not reach its intended assertions on Windows.

The sharing contract is documented by [Microsoft's CreateFileW reference](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew): sharing remains effective until the handle closes, delete sharing governs renames, and write sharing governs other writers. The source has neither sharing permission. This finding is about the new tests; no Windows production restore failure is claimed.

Reproduction to run on Windows: build and run `conversation_file_test` with `STRATA_BUILD_CONVERSATION_TESTS=ON`. The first affected operation is the unconditional `fs::rename` directly after the successful streaming read. Keep the existing Windows file safety contract when making the tests portable; platform-appropriate refusal assertions are acceptable.

## Checks actually run on the reviewed revisions

Passed:

- `g++ -std=c++20 -O2 -Wall -Wextra -Werror -I include src/core/conversation_file_test.cpp src/core/conversation_file.cpp -o /tmp/strata-refinery-stream-restore/conversation_file_test` and the resulting executable: **223 checks passed** on Linux. Covers the golden format, default and buffered I/O, streaming checksums and file replacement/truncation, metadata admission refusal, mutable materialization, and a 256 MiB K/V restore under a 192 MiB address-space cap.
- The F1 probe above reproduced the invalid published file through the real APIs.
- Dotware `bun test dev/model.test.ts`: **3 tests passed**, 7 assertions. The launcher itself is identical at `6698f19` and the current checkout.
- Resolved the actual Dotware preset JSON at `6698f19` with the launcher's merge expression: Frost excludes `--session-restore-stream` and retains `--vision`; Gyro inherits both streaming and vision. The configured local engine exists and its build metadata names source `7eb19f0829b62e36d7ef1ad67c69a30780b064bb`.

Not run / remaining gaps:

- Windows execution or compilation, including the F2 trigger; this remains static analysis.
- A fresh full CUDA, HIP, SYCL or Metal engine build in this review. The CPU-only file test does not establish backend compatibility.
- Live model restore, chunk mutation during GPU transfer, engine fatal-exit/restart behavior after partial device writes, and the documented 177,545-token cold-restore timing/RSS/SHA comparison. The existing production engine was not stopped or restarted.
- An actual preset launch or model switch. Configuration resolution was checked without starting a service.

## Fixing workspace

An isolated local checkout for this PR is available at `/tmp/strata-refinery-stream-restore/repo`. It starts at the exact reviewed Strata head and excludes the owner's unrelated working changes. Other work may be active in the original checkout; do not revert it. Keep code and tests scoped to F1/F2 and commit each meaningful step without attribution lines. The CPU check command above builds from this checkout as well; keep experimental probes outside the repository. Leave this PR as a draft while required platform/backend checks remain unperformed.
