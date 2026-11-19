variable "site_name" {
  type = string
}

resource "aws_s3_bucket" "site" {
  bucket = var.site_name
}

output "regional_name" {
  value = aws_s3_bucket.site.bucket_regional_domain_name
}
