terraform {
  required_version = "1.16.6"

  cloud {
    
    organization = "DigitalTech"

    workspaces {
      name = "aws-automated-incident-remediation"
    }
  }
}

#