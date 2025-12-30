.PHONY: help install bootstrap deploy deploy-services check syntax add-service

VAULT_OPTS := --vault-password-file=.vault_pass

help:
	@echo "Available commands:"
	@echo "  make install         - Install ansible collections"
	@echo "  make bootstrap       - First run on fresh server (uses root)"
	@echo "  make deploy          - Full deployment (uses deploy user)"
	@echo "  make deploy-services - Deploy only services (skip system setup)"
	@echo "  make check           - Dry run to see what would change"
	@echo "  make syntax          - Check playbook syntax"
	@echo "  make add-service     - Add a new service interactively"

install:
	ansible-galaxy collection install -r requirements.yml

bootstrap:
	ansible-playbook $(VAULT_OPTS) playbooks/bootstrap.yml -e ansible_user=root

deploy:
	ansible-playbook $(VAULT_OPTS) playbooks/site.yml

deploy-services:
	ansible-playbook $(VAULT_OPTS) playbooks/site.yml --tags services,caddy

check:
	ansible-playbook $(VAULT_OPTS) playbooks/site.yml --check --diff

syntax:
	ansible-playbook playbooks/site.yml --syntax-check
	ansible-playbook playbooks/bootstrap.yml --syntax-check

add-service:
	@echo "Adding new service to services.yml"
	@read -p "Service name: " name; \
	read -p "Domain (e.g., app.example.com): " domain; \
	read -p "Port: " port; \
	read -p "GitHub repo (owner/repo): " repo; \
	read -p "Branch [main]: " branch; \
	branch=$${branch:-main}; \
	read -p "Compose file [docker-compose.prod.yml]: " compose; \
	compose=$${compose:-docker-compose.prod.yml}; \
	echo "" >> services.yml; \
	echo "  - name: $$name" >> services.yml; \
	echo "    domain: $$domain" >> services.yml; \
	echo "    port: $$port" >> services.yml; \
	echo "    github_repo: $$repo" >> services.yml; \
	echo "    branch: $$branch" >> services.yml; \
	echo "    compose_file: $$compose" >> services.yml; \
	echo ""; \
	echo "Added $$name to services.yml"; \
	echo "Run 'make deploy' or 'make deploy-services' to apply"
