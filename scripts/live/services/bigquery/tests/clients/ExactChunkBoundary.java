import com.google.auth.oauth2.ServiceAccountCredentials;
import com.google.cloud.bigquery.*;
import com.google.cloud.http.HttpTransportOptions;
import java.io.*;
import java.nio.channels.Channels;
import java.nio.file.*;
import java.util.List;

/** A literal one-row oracle, independent of the upload response. */
public final class ExactChunkBoundary {
  public static void main(String[] args) throws Exception {
    var credentials = ServiceAccountCredentials.fromStream(new FileInputStream(args[1]))
        .createScoped(List.of("https://www.googleapis.com/auth/bigquery"));
    var bq = BigQueryOptions.newBuilder().setProjectId("test-project").setHost(args[0])
        .setCredentials(credentials).setTransportOptions(HttpTransportOptions.newBuilder()
            .setConnectTimeout(2000).setReadTimeout(5000).build()).build().getService();
    var table = TableId.of("slt", args[2]);
    var jobId = JobId.of("test-project", args[2] + "_job");
    var queryId = JobId.of("test-project", args[2] + "_read");
    var schema = Schema.of(Field.of("id", StandardSQLTypeName.INT64),
        Field.of("msg", StandardSQLTypeName.STRING));
    // 2 bytes for "1|", 524285 literal x bytes, and one newline = exactly 512 KiB.
    String expected = "x".repeat(512 * 1024 - 3);
    Path input = Files.createTempFile("bigquery-boundary-", ".csv");
    boolean created = false;
    try {
      Files.writeString(input, "1|" + expected + "\n", java.nio.charset.StandardCharsets.UTF_8);
      if (Files.size(input) != 512 * 1024) throw new AssertionError("input byte count");
      bq.create(TableInfo.of(table, StandardTableDefinition.of(schema)));
      created = true;
      var config = WriteChannelConfiguration.newBuilder(table).setSchema(schema)
          .setFormatOptions(CsvOptions.newBuilder().setFieldDelimiter("|")
              .setSkipLeadingRows(0).setNullMarker("NULL").build())
          .setWriteDisposition(JobInfo.WriteDisposition.WRITE_APPEND).build();
      var channel = bq.writer(jobId, config);
      channel.setChunkSize(256 * 1024);
      try (OutputStream stream = Channels.newOutputStream(channel)) {
        Files.copy(input, stream);
      }
      var job = channel.getJob();
      if (job == null || job.getStatus().getError() != null) {
        throw new AssertionError("expected a successful terminal one-row load job");
      }
      JobStatistics.LoadStatistics stats = job.getStatistics();
      if (!Long.valueOf(1).equals(stats.getOutputRows())) {
        throw new AssertionError("expected exactly one loaded row");
      }
      var result = bq.query(QueryJobConfiguration.newBuilder(
          "SELECT id,msg FROM `test-project.slt." + args[2] + "`")
          .setUseLegacySql(false).build(), queryId);
      var rows = result.iterateAll().iterator();
      if (!rows.hasNext()) throw new AssertionError("missing expected row");
      var row = rows.next();
      if (row.get("id").getLongValue() != 1 || !row.get("msg").getStringValue().equals(expected)
          || rows.hasNext()) throw new AssertionError("expected one exact literal row");
    } finally {
      Files.deleteIfExists(input);
      if (created) {
        bq.delete(queryId);
        bq.delete(jobId);
        bq.delete(table);
      }
    }
  }
}
