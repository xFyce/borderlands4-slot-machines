"""UV clipping helpers used while preparing the original cabinet lights."""

def clip_convex(poly, shape):
    for a, b in zip(shape, shape[1:] + shape[:1]):
        if not poly:
            break

        def distance(p):
            return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
        result = []
        last = poly[-1]
        ld = distance(last)
        for point in poly:
            pd = distance(point)
            if (pd >= 0) != (ld >= 0):
                t = ld / (ld - pd)
                result.append(tuple((x + t * (y - x) for x, y in zip(last, point))))
            if pd >= 0:
                result.append(point)
            last = point
            ld = pd
        poly = result
    return poly

def clipped_mesh(geometry, indices, rectangles, shape=None):
    """Clip UV triangles to light footprints, interpolating original attributes."""
    selected = set(indices)
    output = {'vertices': [], 'normals': [], 'uv': [], 'tangents': [], 'triangles': [], 'face_normals': []}

    def edge(poly, axis, bound, greater):
        if not poly:
            return []
        result = []
        last = poly[-1]
        last_in = last[axis] >= bound if greater else last[axis] <= bound
        for point in poly:
            inside = point[axis] >= bound if greater else point[axis] <= bound
            if inside != last_in:
                t = (bound - last[axis]) / (point[axis] - last[axis])
                result.append(tuple((a + t * (b - a) for a, b in zip(last, point))))
            if inside:
                result.append(point)
            last = point
            last_in = inside
        return result
    for off in range(0, len(geometry['triangles']), 3):
        ids = geometry['triangles'][off:off + 3]
        if not all((i in selected for i in ids)):
            continue
        a, b, c = (geometry['vertices'][i] for i in ids)
        u = [x - y for x, y in zip(b, a)]
        v = [x - y for x, y in zip(c, a)]
        face = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        length = sum((x * x for x in face)) ** 0.5
        if length < 1e-08:
            continue
        face = [x / length for x in face]
        if sum((face[axis] * sum((geometry['normals'][i][axis] for i in ids)) for axis in range(3))) < 0:
            face = [-x for x in face]
        uv = [geometry['uv'][i] for i in ids]
        poly = [(p[0] * 1024, p[1] * 1024, *w) for p, w in zip(uv, ((1, 0, 0), (0, 1, 0), (0, 0, 1)))]
        x0 = min((p[0] for p in poly))
        x1 = max((p[0] for p in poly))
        y0 = min((p[1] for p in poly))
        y1 = max((p[1] for p in poly))
        for a, b, c, d in rectangles:
            if x1 <= a or x0 >= c or y1 <= b or (y0 >= d):
                continue
            cut = poly
            for axis, bound, greater in ((0, a, True), (0, c, False), (1, b, True), (1, d, False)):
                cut = edge(cut, axis, bound, greater)
            if shape is not None:
                cut = clip_convex(cut, shape)
            if len(cut) < 3:
                continue
            area = abs(sum((p[0] * q[1] - q[0] * p[1] for p, q in zip(cut, cut[1:] + cut[:1]))))
            if area < 1e-07:
                continue
            first = len(output['vertices'])
            for p in cut:
                output['uv'].append([p[0] / 1024, p[1] / 1024])
                output['face_normals'].append(face)
                for key in ('vertices', 'normals', 'tangents'):
                    v = [sum((p[2 + j] * geometry[key][i][axis] for j, i in enumerate(ids))) for axis in range(3)]
                    if key != 'vertices':
                        length = sum((x * x for x in v)) ** 0.5
                        if length > 1e-08:
                            v = [x / length for x in v]
                    output[key].append(v)
            for i in range(1, len(cut) - 1):
                p, q, r = (cut[0], cut[i], cut[i + 1])
                if abs((q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])) > 1e-07:
                    output['triangles'].extend((first, first + i, first + i + 1))
    return output
