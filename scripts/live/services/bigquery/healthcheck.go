// Health probe for the local emulator's dataset and query APIs.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

func setting(key, fallback string) string {
	if value := os.Getenv(key); value != "" {
		return value
	}
	return fallback
}

func probe() error {
	project := setting("BIGQUERY_PROJECT", "test-project")
	dataset := setting("BIGQUERY_DATASET", "slt")
	base := "http://127.0.0.1:9050/bigquery/v2/projects/" + project
	client := &http.Client{Timeout: 2 * time.Second}
	requests := []struct{ method, url, body string }{
		{"GET", base + "/datasets/" + dataset, ""},
		{"POST", base + "/queries", `{"query":"SELECT 1","useLegacySql":false}`},
	}
	for _, item := range requests {
		req, err := http.NewRequest(item.method, item.url, bytes.NewBufferString(item.body))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		resp, err := client.Do(req)
		if err != nil {
			return err
		}
		body, err := io.ReadAll(io.LimitReader(resp.Body, 65537))
		resp.Body.Close()
		if err != nil || len(body) > 65536 || resp.StatusCode != 200 {
			return fmt.Errorf("%s: status %d, bounded response error %v", item.method, resp.StatusCode, err)
		}
		if item.method == "POST" {
			var result struct {
				JobComplete bool `json:"jobComplete"`
				Rows        []struct{ F []struct{ V string } }
			}
			if err := json.Unmarshal(body, &result); err != nil {
				return err
			}
			if !result.JobComplete || len(result.Rows) != 1 || len(result.Rows[0].F) != 1 || result.Rows[0].F[0].V != "1" {
				return fmt.Errorf("SELECT 1 did not return the expected completed result")
			}
		}
	}
	return nil
}

func main() {
	if err := probe(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
