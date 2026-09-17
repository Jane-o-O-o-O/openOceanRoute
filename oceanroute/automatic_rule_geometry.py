"""Full geographic primitives and bounded continuous-curve screening.

GIS edges are linear in their original longitude/latitude coordinates. Route
curves are WGS84 geodesic/rhumb; their geographic overlay is adaptively rendered.
Distance bounds use ellipsoidal distances and conservative arc-length balls,
not a list of nearest sampled vertices. No local planar distance is reported
as an ellipsoidal distance.
"""
from __future__ import annotations
from dataclasses import dataclass
import heapq
import math

from scipy.optimize import brentq, minimize_scalar
from shapely.geometry import Point, LineString, Polygon, shape
from shapely.affinity import translate

from .geodesy import GEOD, WGS84_A, WGS84_E2, coordinate, inverse, interpolate, longitude_delta, wrap_longitude

R_MAX = 6399600.0  # exceeds every WGS84 meridional/prime-vertical radius


def distance(a, b):
    return abs(GEOD.inv(a[0], a[1], b[0], b[1])[2])


@dataclass
class Curve:
    a: tuple
    b: tuple
    length: float
    kind: str
    start: float = 0.
    end: float = 1.
    kp0: float = 0.
    route_length: float = 0.

    def at(self, t):
        t = self.start + (self.end-self.start)*t
        if self.kind == 'native':
            return (self.a[0]+(self.b[0]-self.a[0])*t, self.a[1]+(self.b[1]-self.a[1])*t)
        return interpolate(*self.a, *self.b, t, self.kind)

    def radius(self, lo, hi):
        if self.kind != 'native':
            return self.length*(self.end-self.start)*(hi-lo)/2
        # Length of a lon/lat-linear curve is bounded by the maximum surface
        # metric over its latitude interval, including native long-way edges.
        p, q = self.at(lo), self.at(hi)
        low, high = sorted((p[1], q[1]))
        near = 0. if low <= 0 <= high else min(abs(low), abs(high))
        return R_MAX*math.hypot(math.radians(q[1]-p[1]), math.radians(q[0]-p[0])*math.cos(math.radians(near)))/2

    def kp(self, t):
        return self.kp0+self.route_length*(self.start+(self.end-self.start)*t)


def native_edge(a, b):
    coordinate(*a[:2]); coordinate(*b[:2])
    return Curve(tuple(a[:2]), tuple(b[:2]), 0., 'native')


def point_curve(p):
    coordinate(*p[:2])
    return Curve(tuple(p[:2]), tuple(p[:2]), 0., 'native')


def nearest(first, second, budget, tolerance):
    """Bound the global minimum over both continuous parameter intervals.

    Every interval lies within a geodesic ball of radius its conservative
    half arc length. Its center-to-center distance gives a rigorous lower
    bound by the metric triangle inequality. A retained actual pair provides
    the upper bound; subdivision stops only at the declared metric gap.
    """
    best = (float('inf'), 0., 0.)
    serial = 0
    queue = []

    def push(a, b, c, d):
        nonlocal best, serial
        budget.add('pairs')
        mid1, mid2 = (a+b)/2, (c+d)/2
        p, q = first.at(mid1), second.at(mid2)
        value = distance(p, q)
        budget.charge(3)
        if value < best[0]: best = (value, mid1, mid2)
        r1, r2 = first.radius(a, b), second.radius(c, d)
        lower = max(0., value-r1-r2)
        def latitude_interval(curve,lo,hi,radius):
            if curve.kind in {'native','rhumb'} or curve.a[1]==curve.b[1]==0:
                return sorted((curve.at(lo)[1],curve.at(hi)[1]))
            center=curve.at((lo+hi)/2)[1]; delta=math.degrees(radius/6335439.)
            return [max(-90.,center-delta),min(90.,center+delta)]
        la=latitude_interval(first,a,b,r1); lb=latitude_interval(second,c,d,r2)
        gap=max(0.,la[0]-lb[1],lb[0]-la[1])
        lower=max(lower,6335439.*math.radians(gap))
        serial += 1
        heapq.heappush(queue, (lower, serial, a, b, c, d, r1, r2))

    for a in (0., 1.):
        for b in (0., 1.):
            budget.charge(1)
            value = distance(first.at(a), second.at(b))
            if value < best[0]: best = (value, a, b)
    # A local minimizer is only an upper-bound witness; complete interval
    # balls still certify the global distance gap below.
    if second.radius(0.,1.)==0 and first.radius(0.,1.)>0:
        def objective(t):
            budget.charge(3)
            return distance(first.at(t),second.at(0.))
        solved=minimize_scalar(objective,bounds=(0.,1.),method='bounded',options={'xatol':1e-14,'maxiter':100})
        if solved.fun<best[0]:best=(float(solved.fun),float(solved.x),0.)
    push(0., 1., 0., 1.)
    while queue and best[0]-queue[0][0] > tolerance:
        lower, _, a, b, c, d, r1, r2 = heapq.heappop(queue)
        if lower >= best[0]-tolerance: continue
        if r1 >= r2:
            mid = (a+b)/2
            if mid == a or mid == b: break
            push(a, mid, c, d); push(mid, b, c, d)
        else:
            mid = (c+d)/2
            if mid == c or mid == d: break
            push(a, b, c, mid); push(a, b, mid, d)
    lower = min(best[0], queue[0][0] if queue else best[0])
    return {'distance_m': best[0], 'lower_bound_m': lower, 'upper_bound_m': best[0],
            'first_fraction': best[1], 'second_fraction': best[2],
            'first': first.at(best[1]), 'second': second.at(best[2])}


def primitives(geometry, path=()):
    """Preserve each recursive primitive path and every polygon hole."""
    if not isinstance(geometry, dict): raise ValueError('GeoJSON geometry must be an object')
    kind = geometry.get('type')
    if kind == 'GeometryCollection':
        rows = geometry.get('geometries')
        if not isinstance(rows, list): raise ValueError('GeometryCollection.geometries must be an array')
        for i, child in enumerate(rows): yield from primitives(child, path+(i,))
    elif kind in {'Point', 'LineString', 'Polygon'}:
        geom = shape(geometry)
        if geom.is_empty or not geom.is_valid: raise ValueError('empty or invalid GeoJSON primitive')
        for a, b in edges(geom): native_edge(a, b)
        if kind == 'Point': coordinate(geom.x, geom.y)
        yield path, geom
    elif kind in {'MultiPoint', 'MultiLineString', 'MultiPolygon'}:
        geom = shape(geometry)
        for i, child in enumerate(geom.geoms):
            if child.is_empty or not child.is_valid: raise ValueError('empty or invalid GeoJSON primitive')
            for a, b in edges(child): native_edge(a, b)
            if child.geom_type == 'Point': coordinate(child.x, child.y)
            yield path+(i,), child
    else: raise ValueError(f'unsupported GeoJSON primitive {kind!r}')



def primitive_records(geometry, path=()):
    """Retain valid siblings when a recursive child is unavailable."""
    if isinstance(geometry,dict) and geometry.get('type')=='GeometryCollection':
        children=geometry.get('geometries')
        if not isinstance(children,list):
            yield path,None,'GeometryCollection.geometries must be an array'
            return
        for i,child in enumerate(children):yield from primitive_records(child,path+(i,))
        return
    if isinstance(geometry,dict) and geometry.get('type') in {'MultiPoint','MultiLineString','MultiPolygon'}:
        kind={'MultiPoint':'Point','MultiLineString':'LineString','MultiPolygon':'Polygon'}[geometry['type']]
        children=geometry.get('coordinates')
        if not isinstance(children,list):
            yield path,None,'Multi-geometry coordinates must be an array'
            return
        for i,child in enumerate(children):yield from primitive_records({'type':kind,'coordinates':child},path+(i,))
        return
    try:
        for p,geom in primitives(geometry,path):yield p,geom,None
    except (ValueError,TypeError,KeyError) as error:
        yield path,None,str(error)


def edges(geom):
    if geom.geom_type == 'LineString':
        yield from zip(geom.coords, list(geom.coords)[1:])
    elif geom.geom_type == 'Polygon':
        for ring in [geom.exterior, *geom.interiors]:
            yield from zip(ring.coords, list(ring.coords)[1:])


def vertex_count(geom):
    return 1 if geom.geom_type == 'Point' else sum(1 for _ in edges(geom))+ (1 if geom.geom_type == 'LineString' else len(geom.interiors)+1)


class Route:
    def __init__(self, project, budget):
        self.project, self.budget = project, budget
        self.points = project['route']['points']
        self.kind = project['route'].get('curve', 'rhumb')
        self.kps, self.legs = [0.], []
        for a, b in zip(self.points, self.points[1:]):
            pa = (a['longitude'], a['latitude']); pb = (b['longitude'], b['latitude'])
            length, _ = inverse(*pa, *pb, self.kind)
            self.legs.append(Curve(pa, pb, length, self.kind, kp0=self.kps[-1], route_length=length))
            self.kps.append(self.kps[-1]+length)
        self.total = self.kps[-1]

    def at(self, kp):
        for curve in self.legs:
            if kp <= curve.kp0+curve.length+1e-7 and curve.length:
                return curve.at(max(0., min(1., (kp-curve.kp0)/curve.length)))
        p = self.points[-1]
        return p['longitude'], p['latitude']

    def curves(self, start, end):
        for index, curve in enumerate(self.legs):
            lo, hi = max(start, curve.kp0), min(end, curve.kp0+curve.length)
            if hi > lo and curve.length:
                count=max(1,math.ceil((hi-lo)/self.budget.config['max_curve_chunk_m']))
                for j in range(count):
                    left=lo+(hi-lo)*j/count; right=lo+(hi-lo)*(j+1)/count
                    yield index, Curve(curve.a, curve.b, curve.length, curve.kind,
                                      (left-curve.kp0)/curve.length, (right-curve.kp0)/curve.length,
                                      curve.kp0, curve.route_length)

    def rendered(self, curve):
        config, budget = self.budget.config, self.budget
        tol = config['geometry_tolerance_m']/8
        rows = []
        def recurse(lo, hi, p, q, level):
            q = (p[0]+longitude_delta(wrap_longitude(p[0]), q[0]), q[1])
            maximum_error = 0.
            for f in (.25, .5, .75):
                actual = curve.at(lo+(hi-lo)*f)
                linear = (p[0]+(q[0]-p[0])*f, p[1]+(q[1]-p[1])*f)
                maximum_error = max(maximum_error, distance(actual, linear))
            budget.charge(6)
            if curve.length*(curve.end-curve.start)*(hi-lo) > config['max_segment_m'] or maximum_error > tol:
                if level > 40: raise ValueError('route rendering cannot resolve declared tolerance')
                mid = (lo+hi)/2; raw = curve.at(mid)
                pm = (p[0]+longitude_delta(wrap_longitude(p[0]), raw[0]), raw[1])
                recurse(lo, mid, p, pm, level+1); recurse(mid, hi, pm, q, level+1)
            else:
                budget.add('vertices'); rows.append((lo, p))
        p, q = curve.at(0.), curve.at(1.)
        recurse(0., 1., p, q, 0)
        budget.add('vertices')
        last = rows[-1][1]
        q = (last[0]+longitude_delta(wrap_longitude(last[0]), q[0]), q[1])
        rows.append((1., q))
        return rows


def aligned(geom, line):
    """Whole-geometry translations; no independent vertex longitude wrapping."""
    center = (line.bounds[0]+line.bounds[2])/2
    source = (geom.bounds[0]+geom.bounds[2])/2
    k = round((center-source)/360)
    for shift in (360*(k-1), 360*k, 360*(k+1)):
        shifted = translate(geom, xoff=shift)
        if shifted.bounds[2] >= line.bounds[0]-.02 and shifted.bounds[0] <= line.bounds[2]+.02:
            yield shifted


def locate(rows, p):
    best = (float('inf'), None)
    for (lo, a), (hi, b) in zip(rows, rows[1:]):
        dx, dy = b[0]-a[0], b[1]-a[1]
        norm = dx*dx+dy*dy
        f = max(0., min(1., ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/norm)) if norm else 0.
        residual = math.hypot(a[0]+f*dx-p[0], a[1]+f*dy-p[1])
        if residual < best[0]: best = (residual, (lo+(hi-lo)*f, lo, hi))
    return best[1]


def contacts(curve, rows, primitive, budget):
    """Full interval overlay, with root-refined proper line intersections."""
    line = LineString([p for _, p in rows])
    result = []
    for geom in aligned(primitive, line):
        boundary = geom.boundary if geom.geom_type == 'Polygon' else geom
        budget.add('pairs'); budget.charge(len(rows)+vertex_count(geom))
        intersection = line.intersection(boundary)
        pieces = list(intersection.geoms) if hasattr(intersection, 'geoms') else [intersection]
        for piece in pieces:
            if piece.is_empty: continue
            if piece.geom_type == 'Point':
                f, lo, hi = locate(rows, (piece.x, piece.y))
                matches = []
                for a, b in edges(geom):
                    edge = LineString([a[:2], b[:2]])
                    if edge.distance(piece) < 1e-9: matches.append((a[:2], b[:2]))
                actual=curve.at(f)
                actual_chart=(piece.x+longitude_delta(wrap_longitude(piece.x),actual[0]),actual[1])
                verified=distance(actual,(piece.x,piece.y))<=1e-6
                angle, event = None, 'touch'
                if len(matches) == 1:
                    a, b = matches[0]
                    dx, dy = b[0]-a[0], b[1]-a[1]
                    def residual(t):
                        p = curve.at(t); x = rows[0][1][0]+longitude_delta(wrap_longitude(rows[0][1][0]), p[0])
                        return (x-a[0])*dy-(p[1]-a[1])*dx
                    rlo, rhi = residual(lo), residual(hi)
                    if rlo*rhi < 0:
                        f = brentq(residual, lo, hi, xtol=1e-14); verified=True
                    p = curve.at(f)
                    px = piece.x+longitude_delta(wrap_longitude(piece.x),p[0]); py = p[1]
                    edgefraction = ((px-a[0])*dx+(py-a[1])*dy)/(dx*dx+dy*dy) if dx or dy else 0.
                    if not -1e-10<=edgefraction<=1+1e-10:verified=False
                    if verified and 1e-10 < edgefraction < 1-1e-10 and 1e-10 < curve.start+(curve.end-curve.start)*f < 1-1e-10:
                        # Incident GIS tangent is the differential of its native
                        # geographic-linear edge, not a 10m averaged V vertex.
                        latitude=math.radians(p[1]); scale=1-WGS84_E2*math.sin(latitude)**2
                        n=WGS84_A/math.sqrt(scale); meridian=WGS84_A*(1-WGS84_E2)/scale**1.5
                        az=math.degrees(math.atan2(n*math.cos(latitude)*dx,meridian*dy))
                        rz=(GEOD.inv(*curve.a,*p)[1]+180)%360 if curve.kind=='geodesic' else inverse(*curve.a,*curve.b,'rhumb')[1]
                        delta = abs((az-rz+180)%360-180)
                        angle = min(delta, 180-delta)
                        event = 'crossing' if angle > 1e-7 else 'touch'
                    if geom.geom_type == 'Polygon' and event == 'crossing':
                        left = curve.at(max(0.,f-1e-6)); right = curve.at(min(1.,f+1e-6))
                        x0=piece.x+longitude_delta(wrap_longitude(piece.x),left[0]); x1=piece.x+longitude_delta(wrap_longitude(piece.x),right[0])
                        inside0=geom.contains(Point(x0,left[1])); inside1=geom.contains(Point(x1,right[1]))
                        event = 'area_entry' if inside1 and not inside0 else 'area_exit' if inside0 and not inside1 else 'touch'
                result.append({'fraction': f, 'event': event, 'angle_deg': angle,'verified':verified})
            elif piece.geom_type == 'LineString':
                fractions=[locate(rows,p)[0] for p in (piece.coords[0],piece.coords[-1])]
                verified=True
                for t in (fractions[0],sum(fractions)/2,fractions[1]):
                    actual=curve.at(t); anchor=piece.coords[0][0]
                    px=anchor+longitude_delta(wrap_longitude(anchor),actual[0])
                    if piece.distance(Point(px,actual[1]))>1e-12:verified=False
                for f in fractions:
                    result.append({'fraction':f,'event':'overlap','angle_deg':None,'verified':verified})
        if geom.geom_type == 'Polygon' and not result:
            inside = line.intersection(geom)
            if not inside.is_empty and geom.contains(line.interpolate(.5, normalized=True)):
                actual=curve.at(.5); px=line.interpolate(.5,normalized=True).x
                actual_x=px+longitude_delta(wrap_longitude(px),actual[0])
                result.append({'fraction':.5,'event':'containment','angle_deg':None,'verified':geom.contains(Point(actual_x,actual[1]))})
    unique = []
    for row in sorted(result,key=lambda r:r['fraction']):
        if not any(abs(row['fraction']-old['fraction'])*curve.length < 1e-5 and row['event']==old['event'] for old in unique): unique.append(row)
    return unique


def closest_primitive(curve, primitive, budget, tolerance):
    # Containment is a zero horizontal separation, not a polygon-boundary
    # distance. A full route/area overlay detects this before edge minimization.
    rows = None
    if primitive.geom_type == 'Polygon':
        # Used with short render pieces or point subjects. Preserve holes.
        if curve.length == 0:
            p=curve.at(0.)
            for geom in aligned(primitive, Point(p)):
                if geom.covers(Point(p)):
                    return {'distance_m':0.,'lower_bound_m':0.,'upper_bound_m':0.,'first_fraction':0.,'second_fraction':0.,'first':p,'second':p}
    candidates = [point_curve((primitive.x,primitive.y))] if primitive.geom_type=='Point' else [native_edge(a,b) for a,b in edges(primitive)]
    best, lower = None, float('inf')
    for target in candidates:
        row=nearest(curve,target,budget,tolerance)
        lower=min(lower,row['lower_bound_m'])
        if best is None or row['distance_m'] < best['distance_m']: best=row
    if best is not None: best['lower_bound_m']=lower
    return best
