package com.webaction.runtime.containers;

import java.io.Serializable;
import java.util.Iterator;

/**
 * Mock of the platform's {@code com.webaction.runtime.containers.IBatch}: the events a
 * {@link ITaskEvent} carries. Same members as the real interface on 5.4.0.6G.
 */
public interface IBatch<T> extends Iterable<T>, Serializable {

    @Override
    Iterator<T> iterator();

    int size();

    boolean isEmpty();

    T first();

    T last();
}
