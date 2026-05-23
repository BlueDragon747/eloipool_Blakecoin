package pool

import (
	"bytes"
	"embed"
	"html/template"
	"io/fs"
	"net/http"
)

// dashboardAssets keeps the operational dashboard self-contained in the Go
// binary while letting the HTML, CSS, and JavaScript live in normal files.
//
//go:embed dashboard/templates/*.html dashboard/static/*
var dashboardAssets embed.FS

var dashboardTemplate = template.Must(template.ParseFS(dashboardAssets, "dashboard/templates/index.html"))

func dashboardStaticHandler() (http.Handler, error) {
	staticFS, err := fs.Sub(dashboardAssets, "dashboard/static")
	if err != nil {
		return nil, err
	}
	return http.FileServer(http.FS(staticFS)), nil
}

func renderDashboardHTML(w http.ResponseWriter) error {
	var buf bytes.Buffer
	if err := dashboardTemplate.ExecuteTemplate(&buf, "index.html", nil); err != nil {
		return err
	}
	_, err := buf.WriteTo(w)
	return err
}
