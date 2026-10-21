terraform {
  required_version = "~> 1.17.0"

  cloud {

    organization = "DigitalTech"

    workspaces {
      name = "aws-automated-incident-remediation"
    }
  }
}

#