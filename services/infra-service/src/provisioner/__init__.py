"""Provisioner module - automated server provisioning and recovery.

This module handles:
- Server provisioning (password reset, OS reinstall, Ansible playbooks)
- Incident recovery with automatic service redeployment
- Integration with Time4VPS API
"""

from .node import run

__all__ = ["run"]
