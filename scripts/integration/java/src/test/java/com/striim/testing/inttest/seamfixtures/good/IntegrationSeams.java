package com.striim.testing.inttest.seamfixtures.good;

import java.util.Map;

import com.striim.testing.inttest.seamfixtures.ModuleSeam;

/** The convention, as a real op would ship it: one static seamFor(String, Map). */
public final class IntegrationSeams {

    public static String lastRequestedType;

    public static Map<String, Object> lastProperties;

    public static Object seamFor(String parameterTypeName, Map<String, Object> properties) {
        lastRequestedType = parameterTypeName;
        lastProperties = properties;
        // Compare on Class.getName(), never a suffix: a nested type arrives as Outer$Inner.
        if (ModuleSeam.class.getName().equals(parameterTypeName)) {
            return new RealSeam(properties);
        }
        if ("int".equals(parameterTypeName)) {
            return Integer.valueOf(7);
        }
        if ("java.lang.CharSequence".equals(parameterTypeName)) {
            return null;   // "pass null, deliberately" -- a recovered position on a first run
        }
        // The contract: an unrecognised type MUST throw, or a typo becomes a silent null.
        throw new IllegalArgumentException("no seam for " + parameterTypeName);
    }

    public static final class RealSeam implements ModuleSeam {
        public final Map<String, Object> sawProperties;

        RealSeam(Map<String, Object> properties) {
            this.sawProperties = properties;
        }

        @Override
        public String describe() {
            return "real";
        }
    }

    /**
     * The channel-method convention: answers a channel call the harness does not
     * implement itself. Keyed on the METHOD NAME, because a reader channel may declare two
     * methods with the same return type.
     */
    public static Object channelValueFor(String methodName, java.util.Map<String, Object> properties) {
        switch (methodName) {
            case "appQualifiedName":
                return properties.get("IntegrationAppQualifiedName");
            case "componentQualifiedName":
                return properties.get("IntegrationComponentQualifiedName");
            case "deferredName":
                // A reference return may legitimately be null: "the platform cannot answer yet"
                // is a real state these accessors model.
                return null;
            case "pageSize":
                return 42;
            default:
                throw new IllegalArgumentException("no channel seam for " + methodName);
        }
    }
}
