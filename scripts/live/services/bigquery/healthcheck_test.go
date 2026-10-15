package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProbeDeletesJobEvenOnWrongResult(t *testing.T) {
	for _, value := range []string{"1", "2"} {
		deleted := false
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			switch r.Method {
			case "DELETE":
				if r.URL.Path != "/jobs/probe-job/delete" {
					t.Errorf("wrong job: %s", r.URL.Path)
				}
				deleted = true
				w.WriteHeader(204)
			case "POST":
				w.Write([]byte(`{"jobComplete":true,"jobReference":{"jobId":"probe-job"},"rows":[{"f":[{"v":"` + value + `"}]}]}`))
			default:
				w.Write([]byte(`{}`))
			}
		}))
		err := probeAt(srv.URL, srv.Client())
		srv.Close()
		if !deleted {
			t.Fatal("health query job retained")
		}
		if (err == nil) != (value == "1") {
			t.Fatalf("wrong health result: %v", err)
		}
	}
}
