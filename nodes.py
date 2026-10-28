"""Optional ComfyUI companion node for emitting stylized log banners and FX cues."""

from __future__ import annotations

from typing import Any, Dict, Tuple


class _AnyType(str):
    """ComfyUI wildcard socket type compatible with any upstream/downstream wire."""

    def __ne__(self, other: object) -> bool:
        return False


ANY_TYPE = _AnyType("*")


class LogBlasterStageCue:
    """Pass-through stage marker that triggers a visual FX pulse in LogBlaster."""

    @classmethod
    def INPUT_TYPES(cls) -> Dict[str, Any]:
        return {
            "required": {
                "banner": (
                    "STRING",
                    {
                        "default": "STAGE CHECKPOINT",
                        "multiline": False,
                    },
                ),
                "fx_trigger": (
                    ["SHOCKWAVE", "GLITCH_BURST", "PHOSPHOR_FLASH", "BANNER_ONLY"],
                    {"default": "SHOCKWAVE"},
                ),
            },
            "optional": {
                "passthrough": (ANY_TYPE,),
                "always_fire": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("passthrough",)
    FUNCTION = "emit_cue"
    OUTPUT_NODE = True
    CATEGORY = "utils/LogBlaster"

    @classmethod
    def IS_CHANGED(
        cls,
        banner: str = "STAGE CHECKPOINT",
        fx_trigger: str = "SHOCKWAVE",
        passthrough: Any = None,
        always_fire: bool = False,
    ) -> Any:
        if always_fire:
            return float("nan")
        return f"{fx_trigger}:{banner}"

    def emit_cue(
        self,
        banner: str = "STAGE CHECKPOINT",
        fx_trigger: str = "SHOCKWAVE",
        passthrough: Any = None,
        always_fire: bool = False,
    ) -> Tuple[Any]:
        clean_banner = (banner or "STAGE CHECKPOINT").replace("\r", " ").replace("\n", " ").strip()
        clean_fx = (fx_trigger or "SHOCKWAVE").strip().upper()
        bar = "=" * max(16, min(64, len(clean_banner) + 12))
        print(f"[LogBlaster:{clean_fx}] {bar}", flush=True)
        print(f"[LogBlaster:{clean_fx}] >>> {clean_banner} <<<", flush=True)
        print(f"[LogBlaster:{clean_fx}] {bar}", flush=True)
        return (passthrough,)


NODE_CLASS_MAPPINGS = {
    "LogBlaster_StageCue": LogBlasterStageCue,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "LogBlaster_StageCue": "LogBlaster Stage Cue (FX Marker)",
}
