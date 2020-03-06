# coding: utf-8

@cython.wraparound(False)
@cython.boundscheck(False)
def graph_acc(dict graph_in, float coeff=25e-6):
    """
    Calculate cumulative drained areas
    for each pixel represented in the input graph ;
    the input represents the connection between outlet pixels
    and inlet pixels at tile borders.

    Parameters
    ----------

    graph_in: dict
        outlet pixel -> destination pixel + received contributing area
        (tile, i, j) -> (tile, ti, tj, area)
        pixel: triple (int, int, int) = (tile id, pixel row, pixel column)
        rows and columns reference the global dataset
        area: local contributing area in pixel count, ie.
        the area drained by the outlet pixel _within_ the tile it belongs to.

    coeff: float
        coefficient to use to convert contributing areas in pixel count
        to real world surfaces in km^2

    Returns
    -------

    areas: dict
        pixel (row, col) -> cumulative drained area in km^2
        rows and columns reference the global dataset
    """

    cdef:

        long tile, i, j, t, ti, tj
        long area, count = 0
        Graph graph
        GraphItem item
        Degree indegree
        Pixel pixel, target
        ContributingPixel value
        PixTracker seen
        CumAreas areas
        PixQueue queue
        GraphIterator it

    for tile, i, j in graph_in:
        
        t, ti, tj, area = graph_in[(tile, i, j)]
        pixel = Pixel(i, j)
        target = Pixel(ti, tj)
        
        graph[pixel] = ContributingPixel(target, area)

        if indegree.count(target) == 0:
            indegree[target] = 1
        else:
            indegree[target] += 1

        count += 1

    it = graph.begin()
    while it != graph.end():
        item = dereference(it)
        if indegree.count(item.first) == 0:
            queue.push_back(item.first)
        preincrement(it)

    with click.progressbar(length=count) as progress:
    
        while not queue.empty():

            pixel = queue.front()
            queue.pop_front()

            if seen.count(pixel) > 0:
                continue

            progress.update(1)
            seen[pixel] = True

            if graph.count(pixel) > 0:

                value = graph[pixel]
                target = value.first
                area = value.second
                indegree[target] -= 1

                if areas.count(target) > 0:
                    areas[target] += areas[pixel] + area*coeff # convert to km^2
                else:
                    areas[target] = areas[pixel] + area*coeff # convert to km^2

                if indegree[target] == 0:
                    queue.push_back(target)

    return areas, indegree