package main

import (
	"regexp"
	"testing"
)

func TestNetProxyAuditEventNamesMatchConvention(t *testing.T) {
	eventPattern := regexp.MustCompile(`^[a-z]+(\.[a-z_]+){2,}$`)
	events := []string{
		"net.fetch.allowed",
		"net.fetch.denied",
		"net.fetch.failed",
		"net.search.allowed",
		"net.search.denied",
		"net.search.failed",
	}

	for _, ev := range events {
		if !eventPattern.MatchString(ev) {
			t.Fatalf("event does not match convention: %s", ev)
		}
	}
}
