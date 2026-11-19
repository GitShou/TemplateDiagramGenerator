terraform {
  required_providers {
    aws = { source = "hashicorp/aws" }
  }
}

variable "enabled" {
  type    = bool
  default = true
}

locals {
  site_name = "example-terraform-storefront"
}

module "storage" {
  source    = "./modules/site"
  site_name = local.site_name
}

resource "aws_s3_bucket" "fallback" {
  bucket = "${local.site_name}-backup"
}

resource "aws_cloudfront_distribution" "site" {
  enabled = var.enabled
  origin {
    origin_id   = "site"
    domain_name = module.storage.regional_name
  }
  origin {
    origin_id   = "fallback"
    domain_name = aws_s3_bucket.fallback.bucket_regional_domain_name
  }
  origin_group {
    origin_id = "site-or-fallback"
    failover_criteria {
      status_codes = [500, 502, 503, 504]
    }
    member { origin_id = "site" }
    member { origin_id = "fallback" }
  }
  default_cache_behavior {
    target_origin_id       = "site-or-fallback"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    cache_policy_id        = "658327ea-f89d-4fab-a63d-7e88639e58f6"
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate { cloudfront_default_certificate = true }
}

resource "aws_route53_record" "site" {
  zone_id = "Z000000EXAMPLE"
  name    = "example.invalid"
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.site.domain_name
    zone_id                = aws_cloudfront_distribution.site.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_lambda_function" "worker" {
  function_name = "example-daily-worker"
  filename      = "example-worker.zip"
  handler       = "index.handler"
  runtime       = "python3.12"
  role          = "arn:aws:iam::123456789012:role/example-function"
}

resource "aws_scheduler_schedule" "daily" {
  name                = "example-daily-worker"
  schedule_expression = "rate(1 day)"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.worker.arn
    role_arn = "arn:aws:iam::123456789012:role/example-scheduler"
  }
}
