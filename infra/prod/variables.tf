variable "tenants" {
  description = "Catalog of Shopify tenants this environment runs jobs against. Each entry gets its own Secrets Manager secret populated manually with DEST_* + DJANGO_* keys."
  type = map(object({
    description = optional(string)
  }))
  default = {}
}

variable "jobs" {
  description = "Schedulable (tenant, command) pairs. Each becomes one ECS task definition + one EventBridge schedule."
  type = map(object({
    tenant   = string
    command  = list(string)
    schedule = string
    timezone = optional(string, "UTC")
    env      = optional(map(string), {})
    enabled  = optional(bool, false)
    cpu      = optional(string, "512")
    memory   = optional(string, "1024")
  }))
  default = {}

  validation {
    condition     = alltrue([for j in var.jobs : contains(keys(var.tenants), j.tenant)])
    error_message = "Every job.tenant must reference a key declared in var.tenants."
  }
}
