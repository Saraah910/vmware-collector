terraform {
  required_providers {
    vcd = {
      source  = "vmware/vcd"
      version = "3.14.2"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
provider "vcd" {
  user                 = var.vcd_user
  password             = var.vcd_password
  auth_type            = "api_token"
  api_token            = var.vcd_api_token
  org                  = "System"
  url                  = "${var.vcd_url}/api"
  # max_retry_timeout  = var.vcd_max_retry_timeout
  allow_unverified_ssl = false
}
