terraform {
  required_version = "~> 1.16.5"

  cloud {

    organization = "DigitalTech"

    workspaces {
      name = "aws-automated-incident-remediation"
    }
  }
}

#