package pool

import (
	"bytes"
	"strings"
	"testing"
)

func TestDashboardTemplateRenders(t *testing.T) {
	var buf bytes.Buffer
	if err := dashboardTemplate.ExecuteTemplate(&buf, "index.html", nil); err != nil {
		t.Fatalf("render dashboard template: %v", err)
	}
	html := buf.String()
	for _, want := range []string{
		"Blakestream Eloipool",
		`/dashboard/static/dashboard.css`,
		`/dashboard/static/dashboard.js`,
		`id="chainStats"`,
		`id="poolStats"`,
	} {
		if !strings.Contains(html, want) {
			t.Fatalf("rendered dashboard missing %q", want)
		}
	}
}

func TestDashboardStaticAssetsEmbedded(t *testing.T) {
	for _, path := range []string{
		"dashboard/static/dashboard.css",
		"dashboard/static/dashboard.js",
	} {
		if _, err := dashboardAssets.ReadFile(path); err != nil {
			t.Fatalf("read embedded asset %s: %v", path, err)
		}
	}
}
