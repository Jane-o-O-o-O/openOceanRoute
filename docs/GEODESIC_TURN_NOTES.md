# Common-vertex course changes: independent 0.10 development review

This note records an actual defect in an existing derived RPL field and its independent targeted verification. It is not a new original-product heading/turn rule, an installation gate, or a claim of Makai algorithm or field accuracy equivalence. Existing versioned 0.9 and earlier release artifacts are unchanged.

## Mathematical contract

For a positive-length incoming leg and a positive-length outgoing leg at the same route vertex, the signed course change is

`turn_deg = wrap180(outgoing_initial_forward_azimuth - incoming_arrival_forward_azimuth)`.

Compass azimuth increases clockwise: a positive turn is toward starboard. `wrap180` represents the signed smaller course change; the physical absolute angle at a U-turn is 180 degrees. Floating inverse rounding can approach that fold from either equivalent signed side, so the independent U-turn test checks absolute 180 degrees.

For a WGS84 geodesic, the incoming initial bearing at its distant starting point is generally different from the incoming arrival bearing at the vertex. `pyproj.Geod.inv` normally returns a **back** azimuth for its second endpoint. Adding 180 degrees and wrapping to a forward compass bearing supplies the incoming arrival direction. The [official pyproj Geod API](https://pyproj4.github.io/pyproj/stable/api/geod.html) documents these azimuth conventions; [PROJ geodesic documentation](https://proj.org/en/stable/geodesic.html) describes the ellipsoidal geodesic calculation.

For a rhumb leg, its constant compass bearing is also its arrival bearing. This preserves the existing rhumb result. Route endpoints have no two-sided course change and retain `null`. A vertex adjacent to a zero-length leg has no tangent for that zero leg and also retains `null`. This change does not infer a corner across duplicate coordinates. A future proximity target called “altercourse” must separately define whether/how repeated-coordinate groups are canonicalized.

## Actual before/after counterexample

Split one WGS84 geodesic from `(0,70)` to `(90,70)` at its half-length point:

| Quantity | Actual independently constructed value |
| --- | --- |
| Intermediate longitude/latitude | `(45.00000000000001,75.57008147661035)` |
| Incoming leg initial azimuth | `46.780360113620375°` |
| Incoming arrival forward azimuth at the intermediate point | `90°` |
| Outgoing initial azimuth at that same point | `90.00000000000001°` |
| True common-vertex course change | `0°` to floating precision |
| Pre-fix `analyze_project(...).rpl[1].turn_deg` | `43.21963988637964°` |
| Corrected result | `0°` |

The pre-fix code subtracted the two initial leg bearings. That subtraction incorrectly counted geodesic azimuth variation as a route corner.

The parent agent corrected `oceanroute/core.py`; this reviewer edited only the new test file and this note. The independent tests do not call production bearing, interpolation or turn helpers to produce expected angles.

## Targeted evidence actually executed

Command in the current macOS/Python development environment:

```text
.venv/bin/python -B -m pytest -o addopts= -q tests/test_geodesic_turns.py
```

- Before the core correction: **9 failed, 6 passed in 0.17 seconds**. All nine geodesic cases exposed the old formula; the three rhumb and three adjacent-zero-leg cases already passed.
- After the parent correction: **15 passed in 0.10 seconds**. No old complete backend suite, browser suite or install verification was rerun by this reviewer.

The 15 cases comprise three continuous geodesics split at a known fraction (northern high latitude, date line and southern high latitude), five prescribed common-vertex turns (`35,-70,179,-179,180` degrees), one true date-line corner, three analytically known rhumb parallel/meridian turns, and three duplicate-coordinate cases. Prescribed-turn cases construct both legs outward from a common vertex with known headings, then reverse the first leg. Their expected turn is the prescribed heading difference, rather than the implementation's inverse formula. The continuous-geodesic cases also check endpoint nulls and unchanged input data.

These are independent geometrical constructions, but pyproj and the production geodesic calculation share PROJ's ellipsoidal engine. They verify the common-vertex convention and its use; they are not an independent reimplementation or field calibration of PROJ.

## Relation to the supplied manual

The supplied MakaiPlan 6.2.0 manual was actually read at physical PDF pages **262–272** (printed pages **254–264**); original physical pages **263–266** were additionally rendered and individually viewed to inspect the rule dialogs.

- Physical 262–266 describe three rule families: crossings, proximity and slopes.
- Physical 264–265 describe proximity around cable bodies, altercourses and cable transitions. This gives a reason to define actual altercourse points carefully.
- Those pages do not establish a same-name heading limit or turn-angle rule, nor disclose an original corner/tangent algorithm. The correction above therefore remains an independent repair of OceanRoute's derived RPL geometry.
- Physical 264's crossing dialog explicitly shows angle **less than**, crossing depth **more than**, and distance to cable body **less than** predicates. The neighboring prose uses “maximum”/“minimum” wording. Future geographic rules must expose explicit comparator semantics rather than infer comparison direction from those words. The screenshot does not establish AND/OR combinations, threshold equality or acute-versus-directed angle policy.

No proprietary file structure, original source algorithm or undisclosed paid interface is inferred from those public/user-supplied descriptions.
