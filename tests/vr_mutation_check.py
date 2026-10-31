"""Mutation check of the VR wall against the HUD harness (a maintainer tool).

Each mutation puts one known defect back into a scratch copy of
web_app/index.html and runs tests/hud_harness.cjs on it. The harness must FAIL
for every one; a mutation that SURVIVES marks a check that does not guard what
spec section S10 says it does. The repository is never modified.

    python tests/vr_mutation_check.py

Needs Node on PATH. The anchors are exact text: when the VR code is edited,
update the mutation it touches in the same change.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MUTATIONS = {
    "resize keeps the old layout scale": (
        "      if (textCanvas._dpr !== layoutScale) {\n        textCanvas._dpr = layoutScale;\n"
        "        state.textDirty = true;\n      }\n      return;",
        "      return;"),
    "scale-only resize not redrawn": (
        "        textCanvas._dpr = layoutScale;\n        state.textDirty = true;\n      }",
        "        textCanvas._dpr = layoutScale;\n      }"),
    "wall keeps 48px of dock room": (
        "let controlsTop = state.vrLayout ? vh : (Number.isFinite(dockTop) && dockTop > 0 ? dockTop : vh - 48);",
        "let controlsTop = Number.isFinite(dockTop) && dockTop > 0 ? dockTop : vh - 48;"),
    "end handler lowers a starting guard": (
        "    if (xr.session !== session) return;\n    xr.session = null;",
        "    if (xr.session !== session) { xr.entering = false; return; }\n    xr.session = null;"),
    "no ended check after the last step": (
        "      refSpace = await session.requestReferenceSpace(\"local\");\n"
        "      if (ended) throw new Error(\"the headset ended the session\");",
        "      refSpace = await session.requestReferenceSpace(\"local\");"),
    "no context-loss check after the last step": (
        "      if (state.contextLost || xr.contextLosses !== lossesAtLayer) {",
        "      if (false) {"),
    "guard lowered before a failed session ends": (
        "      if (session && !ended) { try { await session.end(); } catch (_) {} }\n      xr.entering = false;",
        "      xr.entering = false;\n      if (session && !ended) { try { await session.end(); } catch (_) {} }"),
    "failed entry does not restart the loop": (
        "      showToast(`VR COULD NOT START (${err && err.message ? err.message : err})`);\n"
        "      scheduleNextFrame();",
        "      showToast(`VR COULD NOT START (${err && err.message ? err.message : err})`);"),
    "exit keeps the VR raster": (
        "    state.vrLayout = false;\n    resizePipelines(false);\n    lastDrawMs = 0;",
        "    state.vrLayout = false;\n    lastDrawMs = 0;"),
    "exit keeps the wall": (
        "    xr.refSpace = null;\n    freeVrWall();\n    state.vrLayout = false;",
        "    xr.refSpace = null;\n    state.vrLayout = false;"),
    "exit does not recheck the headset": (
        "    scheduleNextFrame();\n    // A headset that went away during the session hides the button again.\n"
        "    checkVrSupport();",
        "    scheduleNextFrame();"),
    "no devicechange listener": (
        "    navigator.xr.addEventListener(\"devicechange\", checkVrSupport);",
        "    void 0;"),
    "a session's button can be hidden": (
        "btnVR.hidden = !supported && !xr.session && !xr.entering;",
        "btnVR.hidden = !supported;"),
    "an entry's button can be hidden": (
        "btnVR.hidden = !supported && !xr.session && !xr.entering;",
        "btnVR.hidden = !supported && !xr.session;"),
    "a stale support answer counts": (
        "if (ask === vrSupportAsk) btnVR.hidden",
        "if (true) btnVR.hidden"),
    "text limit announces anyway": (
        "if (state.fontSize !== before) vrNote(",
        "if (true) vrNote("),
    "wall failure leaves a black headset": (
        "      showToast(\"VR WALL COULD NOT BE MADE ON THIS GPU\");\n      session.end().catch(() => {});\n      return;",
        "      return;"),
    "wall failure says nothing": (
        "      showToast(\"VR WALL COULD NOT BE MADE ON THIS GPU\");\n",
        ""),
    "a lost context reads as a wall failure": (
        "if (!gl || state.contextLost || gl.isContextLost()) return;",
        "if (!gl || state.contextLost) return;"),
    "gate ignores WebGL2": (
        "const vrPossible = Boolean(gl && typeof navigator",
        "const vrPossible = Boolean(typeof navigator"),
    "window loop allowed while starting or in VR": (
        "    if (document.hidden || state.contextLost || xr.session || xr.entering) {",
        "    if (document.hidden || state.contextLost) {"),
    "window loop allowed in a running session": (
        "    if (document.hidden || state.contextLost || xr.session || xr.entering) {",
        "    if (document.hidden || state.contextLost || xr.entering) {"),
    "a lost context leaves the session running": (
        "      if (xr.session && !xr.entering) xr.session.end().catch(() => {});",
        "      void 0;"),
    "a lost context keeps the wall's GL objects": (
        "      xr.contextLosses += 1;\n      forgetVrGl();",
        "      xr.contextLosses += 1;"),
    "pagehide leaves the session running": (
        "  window.addEventListener(\"pagehide\", () => {\n    if (xr.session) xr.session.end().catch(() => {});\n  });",
        ""),
    "trigger does nothing": (
        "      setPreset(state.preset + 1);\n",
        ""),
}
# The window loop is refused in two places on purpose (renderFrame and
# scheduleNextFrame); these mutations take the same half out of both.
SECOND_EDIT = {
    "window loop allowed while starting or in VR": (
        "        !xr.session && !xr.entering) {", "        true) {"),
    "window loop allowed in a running session": (
        "        !xr.session && !xr.entering) {", "        !xr.entering) {"),
}


def run_harness(work: Path) -> bool:
    result = subprocess.run(["node", str(work / "tests" / "hud_harness.cjs")], capture_output=True,
                            text=True, timeout=180)
    return "HUD_INTEGRATION_OK" in result.stdout


def main() -> int:
    page = (REPO / "web_app" / "index.html").read_text(encoding="utf-8")
    work = Path(tempfile.mkdtemp(prefix="logblaster_vr_mutations_"))
    try:
        (work / "tests").mkdir()
        (work / "web_app").mkdir()
        shutil.copy(REPO / "tests" / "hud_harness.cjs", work / "tests" / "hud_harness.cjs")
        target = work / "web_app" / "index.html"
        target.write_text(page, encoding="utf-8", newline="")
        if not run_harness(work):
            print("the unmutated page fails the harness; fix that first")
            return 2
        survivors = []
        for name, (old, new) in MUTATIONS.items():
            if page.count(old) != 1:
                print(f"ANCHOR    {name}: found {page.count(old)} times; update this mutation")
                survivors.append(name)
                continue
            mutated = page.replace(old, new)
            if name in SECOND_EDIT:
                old2, new2 = SECOND_EDIT[name]
                if mutated.count(old2) != 1:
                    print(f"ANCHOR    {name}: second edit found {mutated.count(old2)} times")
                    survivors.append(name)
                    continue
                mutated = mutated.replace(old2, new2)
            target.write_text(mutated, encoding="utf-8", newline="")
            caught = not run_harness(work)
            print(("CAUGHT    " if caught else "SURVIVED  ") + name)
            if not caught:
                survivors.append(name)
        print(f"{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations caught")
        return 1 if survivors else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
