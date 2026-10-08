package com.striim.testing.inttest.seamfixtures.throwing;

import java.util.Map;

public final class IntegrationSeams {
    public static Object seamFor(String parameterTypeName, Map<String, Object> properties) {
        throw new IllegalArgumentException("scripted DAO needs a Tables property");
    }
}
