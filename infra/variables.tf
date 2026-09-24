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
  default     = ""
  description = "GitHub owner/repository used by Actions, for example my-org/nurserysignal. Empty disables the deploy role until a repo exists."
}

variable "db_name" {
  type    = string
  default = "nurserysignal"
}

variable "db_username" {
  type    = string
  default = "nurserysignal"
}

