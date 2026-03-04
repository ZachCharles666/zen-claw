package main

import (
	"regexp"
	"testing"
)

func TestExecAuditEventNamesMatchConvention(t *testing.T) {
	eventPattern := regexp.MustCompile(`^[a-z]+(\.[a-z_]+){2,}$`)
	events := []string{
		"exec.request.invalid_method",
		"exec.request.invalid",
		"exec.request.denied",
		"exec.request.allowed",
		"exec.request.failed",
		"exec.session.invalid_method",
		"exec.session.denied",
		"exec.session.started",
		"exec.session.list.invalid_method",
		"exec.session.list.denied",
		"exec.session.list.allowed",
		"exec.session.status.denied",
		"exec.session.status.not_found",
		"exec.session.status.allowed",
		"exec.session.kill.denied",
		"exec.session.kill.not_found",
		"exec.session.kill.allowed",
		"exec.session.kill.noop",
		"exec.session.signal.denied",
		"exec.session.signal.invalid",
		"exec.session.signal.not_found",
		"exec.session.signal.allowed",
		"exec.session.signal.noop",
		"exec.session.resize.denied",
		"exec.session.resize.invalid",
		"exec.session.resize.not_found",
		"exec.session.resize.allowed",
		"exec.session.resize.noop",
		"exec.session.read.denied",
		"exec.session.read.not_found",
		"exec.session.read.invalid_query",
		"exec.session.read.allowed",
		"exec.session.route.not_found",
	}

	for _, ev := range events {
		if !eventPattern.MatchString(ev) {
			t.Fatalf("event does not match convention: %s", ev)
		}
	}
}
