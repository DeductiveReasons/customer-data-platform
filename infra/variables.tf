variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Prefix for all resources"
  type        = string
  default     = "cdp"
}

variable "enabled_destinations" {
  description = "Destinations to deliver to. Set each one's secret before enabling it."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for d in var.enabled_destinations : contains(["braze", "crm", "other_apis"], d)])
    error_message = "Destinations must be braze, crm or other_apis."
  }
}

variable "nba_schedule" {
  type    = string
  default = "rate(1 day)"
}

variable "nba_retrain_schedule" {
  type    = string
  default = "rate(7 days)"
}

variable "nba_schedule_enabled" {
  description = "Run the next best action scan and model retraining on a schedule"
  type        = bool
  default     = false
}

variable "alert_email" {
  description = "Optional email address for alarm notifications"
  type        = string
  default     = ""
}

variable "raw_retention_days" {
  type    = number
  default = 730
}

variable "archive_retention_days" {
  type    = number
  default = 30
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "api_rate_limit" {
  type    = number
  default = 100
}

variable "api_burst_limit" {
  type    = number
  default = 200
}

variable "deletion_protection" {
  description = "Protect the profiles table from deletion"
  type        = bool
  default     = true
}
