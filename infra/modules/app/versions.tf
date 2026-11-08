terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
      # Aliases are configured by the environment root (infra/dev, infra/prod) and passed in.
      configuration_aliases = [aws.us_east_1, aws.dr]
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}
