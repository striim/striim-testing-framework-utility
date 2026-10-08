package com.striim.testing.inttest.seamfixtures.wrongtype;

import java.util.Map;

public final class IntegrationSeams {
    public static Object seamFor(String parameterTypeName, Map<String, Object> properties) {
        return "not a ModuleSeam";
    }
}
