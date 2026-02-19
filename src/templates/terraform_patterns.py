"""
Terraform reference patterns for the agent to follow when generating configurations.
"""


def get_terraform_reference(provider: str = "azurerm") -> str:
    """Return Terraform best-practice reference patterns for the given provider."""
    if provider == "azurerm":
        return _get_azurerm_patterns()
    elif provider == "aws":
        return _get_aws_patterns()
    else:
        return _get_azurerm_patterns()  # Default to Azure


def _get_azurerm_patterns() -> str:
    return """
#### Provider & Backend Pattern
```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.100"
    }
  }

  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "stterraformstate"
    container_name       = "tfstate"
    key                  = "infraforge.tfstate"
  }
}

provider "azurerm" {
  features {
    key_vault {
      purge_soft_delete_on_destroy = false
    }
  }
}
```

#### Variables Pattern
```hcl
variable "environment" {
  description = "The deployment environment (dev, staging, prod)"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "location" {
  description = "Azure region for all resources"
  type        = string
  default     = "eastus2"
}

variable "project_name" {
  description = "Project name used for resource naming"
  type        = string
  validation {
    condition     = length(var.project_name) >= 2 && length(var.project_name) <= 12
    error_message = "Project name must be 2-12 characters."
  }
}

variable "sql_admin_password" {
  description = "SQL Server administrator password"
  type        = string
  sensitive   = true
}

locals {
  resource_prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    environment = var.environment
    project     = var.project_name
    managed_by  = "InfraForge"
    deployed_at = timestamp()
  }
}
```

#### Resource Group Pattern
```hcl
resource "azurerm_resource_group" "main" {
  name     = "rg-${local.resource_prefix}"
  location = var.location
  tags     = local.common_tags
}
```

#### App Service Pattern
```hcl
resource "azurerm_service_plan" "main" {
  name                = "${local.resource_prefix}-asp"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  os_type             = "Linux"
  sku_name            = var.environment == "prod" ? "P1v3" : "B1"
  tags                = local.common_tags
}

resource "azurerm_linux_web_app" "main" {
  name                = "${local.resource_prefix}-app"
  resource_group_name = azurerm_resource_group.main.name
  location            = azurerm_resource_group.main.location
  service_plan_id     = azurerm_service_plan.main.id
  https_only          = true
  tags                = local.common_tags

  identity {
    type = "SystemAssigned"
  }

  site_config {
    minimum_tls_version = "1.2"
    ftps_state          = "Disabled"
    always_on           = var.environment == "prod"
  }

  lifecycle {
    ignore_changes = [site_config[0].application_stack]
  }
}
```

#### Outputs Pattern
```hcl
output "app_service_url" {
  description = "The default URL of the App Service"
  value       = "https://${azurerm_linux_web_app.main.default_hostname}"
}

output "resource_group_name" {
  description = "The name of the resource group"
  value       = azurerm_resource_group.main.name
}

output "app_identity_principal_id" {
  description = "The principal ID of the App Service managed identity"
  value       = azurerm_linux_web_app.main.identity[0].principal_id
}
```
"""


def _get_aws_patterns() -> str:
    return """
#### Provider & Backend Pattern
```hcl
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket         = "my-terraform-state"
    key            = "infraforge.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.common_tags
  }
}
```

#### Variables Pattern
```hcl
variable "environment" {
  description = "The deployment environment"
  type        = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "Environment must be dev, staging, or prod."
  }
}

variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project name for resource naming"
  type        = string
}

locals {
  resource_prefix = "${var.project_name}-${var.environment}"
  common_tags = {
    Environment = var.environment
    Project     = var.project_name
    ManagedBy   = "InfraForge"
  }
}
```
"""
