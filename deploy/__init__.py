"""
deploy/__init__.py
==================
Unified Paper Trade & Live Trade Deployment Engine (1-Click Actionable Alerts +
Kill-Switch / Risk-Limit Gated Auto-Execute).
"""

from deploy.manager import DeploymentManager, deployment_manager

__all__ = ["DeploymentManager", "deployment_manager"]
