"""Conversion of KHR_materials_pbrSpecularGlossiness (legacy) to metal-rough.

Sketchfab / Substance Painter (pre-2018) / Marmoset etc. emit
spec-gloss-only glTFs. The modern spec deprecated it but real-world assets
still ship with it. Most free Maya glTF tools (and the paid parashivbrl
plugin) don't handle this — easy differentiator.

The conversion is mathematically approximate. The formula below is the
"Babylon.js / Khronos reference" approach (Lambert-based luminance match)
which is the most widely-cited solution.

Reference: https://github.com/KhronosGroup/glTF/blob/main/extensions/2.0/Archived/KHR_materials_pbrSpecularGlossiness/examples/convert-between-workflows-bjs/js/index.js
"""
from __future__ import annotations

import math


# Threshold below which we treat a material as a pure dielectric
DIELECTRIC_SPECULAR_THRESHOLD = 0.04
EPSILON = 1e-6


def _solve_metallic(diffuse_lum, specular_lum, one_minus_specular_strength):
    """Solve the quadratic for metallic per the Babylon.js conversion.

    Returns metallic value in [0, 1]. Standard dielectrics give 0; pure
    metals give 1; in-between for tinted metals.
    """
    if specular_lum < DIELECTRIC_SPECULAR_THRESHOLD:
        return 0.0
    a = DIELECTRIC_SPECULAR_THRESHOLD
    b = (diffuse_lum * one_minus_specular_strength
         / (1.0 - DIELECTRIC_SPECULAR_THRESHOLD)
         + specular_lum
         - 2.0 * DIELECTRIC_SPECULAR_THRESHOLD)
    c = DIELECTRIC_SPECULAR_THRESHOLD - specular_lum
    disc = b * b - 4.0 * a * c
    if disc < 0.0:
        disc = 0.0
    metal = (-b + math.sqrt(disc)) / (2.0 * a)
    return max(0.0, min(1.0, metal))


def _perceived_brightness(rgb):
    """ITU-R BT.601 perceived luminance — what humans see, what the
    spec-gloss conversion is calibrated against."""
    r, g, b = rgb[0], rgb[1], rgb[2]
    return math.sqrt(0.299 * r * r + 0.587 * g * g + 0.114 * b * b)


def specgloss_to_metalrough(ext):
    """Convert a KHR_materials_pbrSpecularGlossiness extension block to a
    pbrMetallicRoughness-shaped dict.

    Texture references are passed through with a note in their name — the
    actual per-pixel conversion would need shader-graph math, which we don't
    do in v0.1. Scalar factors are computed exactly.

    Returns a dict matching the glTF pbrMetallicRoughness schema.
    """
    diffuse = ext.get("diffuseFactor", [1.0, 1.0, 1.0, 1.0])
    specular = ext.get("specularFactor", [1.0, 1.0, 1.0])
    glossiness = ext.get("glossinessFactor", 1.0)

    # Per-channel conversion of factors
    one_minus_specular = 1.0 - max(specular[0], specular[1], specular[2])
    diff_lum = _perceived_brightness(diffuse)
    spec_lum = _perceived_brightness(specular)
    metal = _solve_metallic(diff_lum, spec_lum, one_minus_specular)

    # Compute base color
    if metal < EPSILON:
        # Dielectric — base color from diffuse
        base_color = [diffuse[0], diffuse[1], diffuse[2], diffuse[3]]
    else:
        # Metallic — base color blends toward specular
        base_color = [
            (specular[0] - DIELECTRIC_SPECULAR_THRESHOLD * (1.0 - metal)) / max(metal, EPSILON),
            (specular[1] - DIELECTRIC_SPECULAR_THRESHOLD * (1.0 - metal)) / max(metal, EPSILON),
            (specular[2] - DIELECTRIC_SPECULAR_THRESHOLD * (1.0 - metal)) / max(metal, EPSILON),
            diffuse[3],
        ]
        # Clamp
        for i in range(3):
            base_color[i] = max(0.0, min(1.0, base_color[i]))

    out = {
        "baseColorFactor": base_color,
        "metallicFactor": metal,
        "roughnessFactor": 1.0 - glossiness,
    }

    # Pass through textures so the material isn't all-default-color
    if "diffuseTexture" in ext:
        # The diffuse texture becomes the base color texture (approximate;
        # for metallic materials the texture should be reblended toward the
        # spec texture — we don't do that per-pixel in v0.1)
        out["baseColorTexture"] = ext["diffuseTexture"]

    # specularGlossinessTexture is more complex — its RGB is the per-pixel
    # specular color and A is glossiness. We'd need a shader-graph split to
    # use it correctly. v0.1 ignores it and uses the scalar metallicFactor /
    # roughnessFactor from above.

    return out
