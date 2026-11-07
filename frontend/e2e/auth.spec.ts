import { expect, test } from '@playwright/test'

import { ssoLogin } from './helpers'

test.describe('authentication', () => {
  test('reviewer signs in via mock SSO + 2FA and lands on their queue', async ({ page }) => {
    await ssoLogin(page, 'jordan.lee@acmepay.com')
    await expect(page.getByRole('link', { name: 'My queue' })).toBeVisible()
    await expect(page.getByRole('heading', { name: /Good to see you/ })).toBeVisible()
    await expect(page.getByText('jordan.lee@acmepay.com')).toBeVisible()
  })

  test('wrong 2FA code is rejected and shows an error', async ({ page }) => {
    await page.goto('/')
    await page.getByTestId('sso-login').click()
    await page.locator('button', { hasText: 'sam.patel@acmepay.com' }).click()
    await page.locator('input[name="code"]').fill('000000')
    await page.getByRole('button', { name: 'Verify & sign in' }).click()
    await expect(page.getByText('Incorrect verification code')).toBeVisible()
    // Still on the IdP page — no redirect back to the app.
    await expect(page.locator('input[name="code"]')).toBeVisible()
  })

  test('sign out returns to the login screen', async ({ page }) => {
    await ssoLogin(page, 'vik.auditor@acmepay.com')
    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByTestId('sso-login')).toBeVisible()
  })

  test('unauthenticated API calls are rejected', async ({ page }) => {
    const res = await page.request.get('/api/auth/me')
    expect(res.status()).toBe(401)
  })
})
