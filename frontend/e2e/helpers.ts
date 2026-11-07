import { expect, type Page } from '@playwright/test'

/** Full mock-SSO sign-in: SPA login button → IdP identity picker → mock 2FA (123456) → back to the app. */
export async function ssoLogin(page: Page, email: string) {
  await page.goto('/')
  await page.getByTestId('sso-login').click()
  // Mock IdP identity picker (served by the API, same origin via /api proxy or nginx).
  await page.locator('button', { hasText: email }).click()
  // Mock second factor.
  await page.locator('input[name="code"]').fill('123456')
  await page.getByRole('button', { name: 'Verify & sign in' }).click()
  // Redirect back to the SPA with ?code=…, exchanged for a session token.
  await expect(page).not.toHaveURL(/\?code=/)
  await expect(page.locator('.topbar .name')).toBeVisible()
}
