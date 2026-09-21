# Rhumb divided differences and polar numerical review

This records an independent 0.11 numerical review of `oceanroute/geodesy.py` and the automatic-rule continuous-curve distance/work contract. It does not certify chart accuracy, manufacturer equivalence, or GeographicLib's published accuracy for this separate implementation. Earlier release artifacts were not changed.

## Metric and cancellation

For WGS84 geodetic latitude `phi`, equatorial radius `a`, and squared eccentricity `e2`, define

```
psi(phi) = asinh(tan(phi)) - e*atanh(e*sin(phi))
M'(phi)  = a*(1-e2)/(1-e2*sin(phi)^2)^(3/2)
psi'(phi)= (1-e2)/(cos(phi)*(1-e2*sin(phi)^2))
Q        = delta_psi/delta_M
distance = hypot(delta_lambda, delta_psi)/abs(Q)
heading  = atan2(delta_lambda, delta_psi)
```

Angles inside these formulas are radians; heading is a signed compass angle normalized to `[0,360)`. The longitude difference selects the existing shortest signed longitude interval, retaining direction at exactly 180 degrees. Constant rhumb heading makes meridian arc advance linearly with physical distance. Thus the requested position satisfies `M(phi_f)-M(phi1)=f*(M(phi2)-M(phi1))`; longitude then advances by the actual isometric-latitude fraction. A constant radial or latitude fraction would not generally be a physical-length fraction.

For a near-parallel interval, subtracting two rounded native meridian inverses can destroy its small latitude increment. Write both differences as integrals on the same normalized interval. Their common `delta_phi` cancels **before** division:

```
Q = integral_0^1 psi'(phi1+t*delta_phi) dt
    / integral_0^1 M'(phi1+t*delta_phi) dt.
```

The implementation evaluates these smooth integrals with four-point Gauss-Legendre quadrature when `abs(delta_phi) < .001*abs(cos(midlatitude))`. Its weights have a common scale which cancels in the ratio. This is an approximation to the divided difference, not GeographicLib's exact/series rhumb algorithm or a certified interval remainder calculation. The restricted width makes the integrands slowly varying relative to their polar scale; ordinary floating-point evaluation and native coordinate rounding still remain.

Near-parallel interpolation uses `f*Q(phi1,phi_f)/Q(phi1,phi2)` for the longitude fraction. This avoids dividing a quantized returned latitude increment by an almost-zero end-to-end increment. The returned latitude still comes from native meridian advancement. It cannot represent sub-ULP changes in a binary64 latitude, and the combination must not be advertised as guaranteeing nanometre output coordinates.

## Actual polar failure and degree-complement repair

The previous wide-interval spherical difference formed

```
ratio = (sin(phi2)-sin(phi1))/(1-sin(phi1)*sin(phi2))
delta_psi_spherical = atanh(ratio)
```

The trigonometric divided-difference identity avoids subtraction for small changes, but `atanh` is ill-conditioned when `ratio` is close to either one. Testing only `abs(ratio)<1` does not establish accuracy. In the actual `(0,80) -> (180,89.999999)` input, `1-ratio` was `1.9872992140790302e-14`; the resulting `delta_psi` was `16.121178594527876`, whereas an 80-decimal-digit oracle gave `16.120536274703647...`. The error was present before the ratio rounded to one.

The actual pre-repair outputs were:

| Input in longitude/latitude order | Actual length error | Actual `f=.37` position error |
|---|---:|---:|
| `(0,80,180,89.999999)` | `-1.658739399 m` | `2.527498626 m` |
| `(0,0,180,nextafter(90,0))` | `+267.6180830 m` | `1025.661133 m` |

Here `nextafter(90,0)` is the legitimate finite input `89.99999999999999`, not the exact pole. The second case also showed the failure of absolute `tan(radians(latitude))` close to a pole: rounding the latitude in radians changes its already tiny polar complement by a large relative amount.

The repaired degree wrapper forms `theta=radians(90-abs(latitude))` before trigonometric evaluation. Away from the ordinary central latitude domain, the spherical isometric latitude is `sign(latitude)*[-log(tan(theta/2))]`; the ellipsoidal correction uses `cos(theta)`. Wide intervals subtract these stable absolute values, rather than an almost-unit `atanh` ratio. High-latitude near-interval quadrature also forms its interpolated polar complement in degrees and uses `sin(theta)` for `cos(phi)`. Native meridian calls receive the original degree inputs without a radians-to-degrees round trip. The old radian helpers retain their narrower compatibility contract; a radian value that has already discarded its polar complement cannot reconstruct the original input degrees.

Exact pole endpoints retain the explicit application policy: nonmeridian rhumb connections are rejected, while a meridian can reach a pole. This review does not make a through-pole longitude meaningful or add multirevolution rhumb routes.

## Independent evidence and retained accuracy limit

`tests/fixtures/rhumb_polar_oracles.json` was actually generated with mpmath 1.3.0 at 80 decimal digits. It records WGS84 constants, the generating equations, precise decimal results, and `float.hex()` for every input. `mp.mpf` received the original binary64 numbers, rather than silently replacing them with their decimal spellings. The oracle directly evaluates high-precision isometric latitude, integrates the meridian derivative, and solves the monotone meridian equation using its analytic derivative. It imports no production geodesy or route helper.

The 16 fixtures include both closest representable poles, northern/southern wide intervals, reversal, a cross-polar interval, almost-parallel intervals, both sides of the normalized branch threshold, and date-line routes. Four actual physical fractions are recorded per fixture. The ordinary-pole parallel limit additionally uses the exact ellipsoidal parallel metric; its relative check detects the former approximately 14% relative error in a very small polar circumference. Tests consume the committed finite JSON and do not require mpmath during installation. PROJ is shared only to measure distance between actual coordinates and the already independently calculated, rounded expected coordinates.

Actual targeted execution after the repair:

- `tests/test_rhumb_polar.py`: **38 passed in 0.04 s**.
- The new file, existing 22 precision cases, and existing 18 geodesy cases together: **78 passed in 0.33 s**, exit 0.
- Across the 16 golden cases, the largest observed length error was `1.941807568e-7 m`, heading error `2.017941370e-12 degrees`, and physical-position error `2.339503061e-6 m`. The position maximum occurred just outside the near-interval threshold at latitude 85 degrees with a 180-degree longitude difference.

The actual test allowances are one micrometre for nondegenerate inverse lengths, `1e-9 degrees` for inverse heading, and three micrometres for output-coordinate displacement in this fixture domain. These are supported numerical acceptance limits for the named fixtures, **not an all-input upper bound**. Separate preliminary 70-digit comparisons around the old branch boundary also showed micrometre, rather than universal ten-nanometre, behavior. Public GeographicLib rhumb documentation's approximately 10 nm figure belongs to its own algorithm. It must not be transferred to this Gauss/native hybrid, or confused with nautical miles, bathymetric accuracy, manufacturing accuracy, or field positioning accuracy.

The 78-case run observed `geodesy.py` SHA256 `130f7af0d043203f868a3699f037da1f1e6471595b5d269f838079b3dfe4f76f`. It is a source-bound module check, not the 0.11 whole-backend, browser, wheel, or fresh-install gate. Later release checks need their own actual frozen source evidence.

## Automatic-rule circle balls and charged work

For the admitted WGS84 geodesic-radius circle domain, positive curvature bounds reduced length by the radial distance, so `R*abs(sweep_rad)` bounds the full circle-arc length. A physical-fraction interval of width `df` has length no greater than that bound times `df`; its midpoint therefore has an enclosing metric-ball radius `upper_length*df/2`. The automatic-rule `Curve.radius` additionally includes the explicit arc position allowance. Interval bounds use the metric triangle inequality against an actual pair of points. They do not replace the original circle with a chord or apply the rhumb/equatorial latitude shortcut to an arc whose endpoints happen to share a latitude.

The geometric inequality is mathematical; binary64 direct/inverse engines, quadrature, inversion, and the documented position allowance are not an interval-arithmetic rounding proof. Straight rhumb/geodesic branches do not acquire the circle's position allowance. An observed micrometre error much smaller than a configured millimetre screening tolerance is useful evidence, but is not a proof for every admitted coordinate. The polar error above mattered to length-based balls as well as map positions, so it was reported to the parent instead of weakening independent assertions.

An actual 500 m semicircle consumed 25 native construction evaluations. Under the shared job budget, `Curve.at(.37)` consumed and charged another 22 native evaluations; `Curve.tangent(.37)` consumed and charged one. Caps 50, 70, 100 and 1000 all reported equal outer/native increments. At caps 25, 26 and 30, the actual position calculation rejected the entire job with `AUTOMATIC_RULE_WORK_LIMIT`, without returning a truncated report. A direct one-unit primitive constructor also rejected. Constructor and per-call remaining allowances are propagated to the actual arc solver, and subcurves share its cumulative counter.

These units are native/logical evaluations, not FLOPs, token billing, CPU time, or RSS guarantees. Workspace/schema and pure-source admission remain the expressly excluded preceding costs; a per-segment intrinsic numerical cap can also reject before a larger job cap is exhausted. Failed calculations are not certified-clear results.

## Primary references

- [Karney, The area of rhumb polygons](https://arxiv.org/abs/2303.03219), including divided differences for near east/west rhumb computation.
- [Official GeographicLib RhumbSolve manual source](https://raw.githubusercontent.com/geographiclib/geographiclib/main/man/RhumbSolve.pod), for rhumb/pole semantics, units, and the accuracy statement of that implementation.
- [Official GeographicLib Rhumb source](https://raw.githubusercontent.com/geographiclib/geographiclib/main/src/Rhumb.cpp), for the separate library's implementation context.
- [GeographicLib geodesic definitions and reduced length](https://geographiclib.sourceforge.io/html/python/geodesics.html), and [PROJ Geod API](https://pyproj4.github.io/pyproj/stable/api/geod.html).
