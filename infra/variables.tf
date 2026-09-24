variable "aws_region" {
  type    = string
  default = "eu-west-1"
}

variable "aws_profile" {
  type    = string
  default = "nurserysignal"
}

variable "project_name" {
  type    = string
  default = "nurserysignal"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "github_repository" {
  type        = string
  default     = "gaspode/NurserySignal"
  description = "GitHub owner/repository used by Actions."
}

variable "github_oidc_subject_prefix" {
  type        = string
  default     = "gaspode@856445/NurserySignal@1385020285"
  description = "Immutable GitHub OIDC subject prefix for gaspode/NurserySignal."
}

variable "db_name" {
  type    = string
  default = "nurserysignal"
}

variable "db_username" {
  type    = string
  default = "nurserysignal"
}
