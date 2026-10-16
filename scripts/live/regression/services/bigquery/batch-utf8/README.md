# UTF-8 batch control

UTF-8 batch control with a pipe, doubled quote and newline. The golden is the literal source row. Set `SLT_STRIIM_JAVA_SYSTEM_PROPERTIES=-Dfile.encoding=UTF-8` for this run: the writer formatter inherits the JVM default charset before compression. Its BigQuery Encoding setting alone does not override that formatter. Run doctor and run with this same setting.
