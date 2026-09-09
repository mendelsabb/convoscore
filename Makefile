# ConvoScore — one documented path to bring the whole system up and down.
#
# This Makefile is a thin dispatcher: all logic lives in scripts/*.sh so it works with the
# GNU make 3.81 / bash 3.2 that ship with macOS. Run `make help` for the list of targets.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Exported so scripts/*.sh read them from the environment (works on GNU make 3.81).
COMPONENT ?= worker
TARGET ?= worker
export COMPONENT
export TARGET

# $(call run,<script-name>) runs scripts/<script-name>.sh, or explains that it does not exist yet.
define run
@if [ -x "scripts/$(1).sh" ]; then scripts/$(1).sh; else echo "scripts/$(1).sh is not implemented yet (see README Status)"; exit 1; fi
endef

.PHONY: help up down status deploy test demo-data demo-infra-failure demo-restart logs

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

up: ## Bring everything up: kind, LocalStack, Terraform, monitoring, images, Helm release
	$(call run,up)

down: ## Tear everything down (cluster, Terraform state)
	$(call run,down)

status: ## Show pods, URLs and health
	$(call run,status)

deploy: ## Rebuild images and upgrade the Helm release only (fast inner loop)
	$(call run,deploy)

test: ## Run backend tests (PostgreSQL via docker compose, AWS mocked) and frontend checks
	$(call run,test)

demo-data: ## Submit sample conversations via the API and upload some to S3
	$(call run,demo-data)

demo-infra-failure: ## Delete a pod (TARGET=worker|api|postgres) and watch Kubernetes recover
	$(call run,demo-infra-failure)

demo-restart: ## Restart every component and prove results survived
	$(call run,demo-restart)

logs: ## Tail logs (COMPONENT=api|worker|ingestor|web)
	$(call run,logs)
