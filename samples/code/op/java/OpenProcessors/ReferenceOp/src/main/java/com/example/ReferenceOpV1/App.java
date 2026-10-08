package com.example.ReferenceOpV1;

import java.util.Map;

import com.webaction.anno.AdapterType;
import com.webaction.anno.PropertyTemplate;
import com.webaction.anno.PropertyTemplateProperty;
import com.example.common.AbstractOpenProcessorApp;
import com.example.common.BuiltInFuncResolver;
import com.example.common.EventProcessor;

// The @PropertyTemplate name is a four-way contract: it must equal the artifactId, the package
// leaf (com.example.ReferenceOpV1), and the pom's OP_VERSION suffix. Striim keys its OP cache
// on that version suffix, so bumping one of the four without the others loads a stale operator.
@PropertyTemplate(name = "ReferenceOpV1", type = AdapterType.process, properties = {
        @PropertyTemplateProperty(name = "EnableLogging", label = "Enable Logging", description = "Enable Logging", type = Boolean.class, required = false, defaultValue = "false"),
        @PropertyTemplateProperty(name = "EnableInspection", label = "Enable Inspection", description = "Record on each emitted event whether the source supplied each column, as one column N userdata entry per column", type = Boolean.class, required = false, defaultValue = "false")
}, inputType = com.webaction.proc.events.WAEvent.class, outputType = com.webaction.proc.events.WAEvent.class)
public class App extends AbstractOpenProcessorApp {

    @Override
    protected EventProcessor<com.webaction.proc.events.WAEvent> buildProcessor(Map<String, Object> props) {
        return new Processor(props, new BuiltInFuncResolver(), logger);
    }
}
