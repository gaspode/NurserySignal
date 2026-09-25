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

variable "ai_shadow_enabled" {
  type        = bool
  default     = true
  description = "Enable non-authoritative Bedrock shadow assessments for new enrichment messages."
}

variable "ai_model_id" {
  type        = string
  default     = "amazon.nova-lite-v1:0"
  description = "Bedrock model ID used by the enrichment shadow assessment."
}

variable "ai_prompt_version" {
  type        = string
  default     = "shadow-v1"
  description = "Version of the shadow review prompt."
}
